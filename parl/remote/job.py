#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
os.environ['CUDA_VISIBLE_DEVICES'] = ''
os.environ['XPARL'] = 'True'
import argparse
import cloudpickle
import pickle
import sys
import tempfile
import threading
import time
import traceback
import zmq
from parl.utils import to_str, to_byte, get_ip_address, logger
from parl.utils.communication import loads_argument, loads_return,\
    dumps_argument, dumps_return
from parl.remote import remote_constants
from parl.utils.exceptions import SerializeError, DeserializeError
from parl.remote.message import InitializedJob


class Job(object):
    """Base class for the job.

    After establishing connection with the remote object, the job will
    create a remote class instance locally and enter an infinite loop,
    waiting for commands from the remote object.

    """

    def __init__(self, worker_address, master_address):
        """
        Args:
            worker_address(str): worker_address for sending job information(e.g, pid)
            master_address(str): master_address for letting the master know that the job is ready for the new task.(e.g, pid)
        """
        self.job_is_alive = True
        self.worker_address = worker_address
        self.master_address = master_address
        self._create_sockets()

    def _create_sockets(self):
        """Create three sockets for each job.

        (1) reply_socket(main socket): receives the command(i.e, the function name and args) 
            from the actual class instance, completes the computation, and returns the result of
            the function.
        (2) job_socket(functional socket): sends job_address and heartbeat_address to worker.
        (3) master_socket(functional socket): lets the master know that this job is ready for a new task.

        """

        self.ctx = zmq.Context()

        # create the reply_socket
        self.reply_socket = self.ctx.socket(zmq.REP)
        job_port = self.reply_socket.bind_to_random_port(addr="tcp://*")
        self.reply_socket.linger = 0
        self.job_ip = get_ip_address()
        self.job_address = "{}:{}".format(self.job_ip, job_port)

        # create the job_socket
        self.job_socket = self.ctx.socket(zmq.REQ)
        self.job_socket.connect("tcp://{}".format(self.worker_address))

        # create the master_socket
        self.master_socket = self.ctx.socket(zmq.REQ)
        self.master_socket.connect("tcp://{}".format(self.master_address))

        # a thread that reply ping signals from the client
        ping_heartbeat_socket, ping_heartbeat_address = self._create_heartbeat_server(
            timeout=False)
        ping_thread = threading.Thread(
            target=self._reply_ping, args=(ping_heartbeat_socket, ))
        ping_thread.setDaemon(True)
        ping_thread.start()
        self.ping_heartbeat_address = ping_heartbeat_address

        # a thread that reply heartbeat signals from the worker
        worker_heartbeat_socket, worker_heartbeat_address = self._create_heartbeat_server(
        )
        worker_thread = threading.Thread(
            target=self._reply_worker_heartbeat,
            args=(worker_heartbeat_socket, ))
        worker_thread.setDaemon(True)
        worker_thread.start()

        # a thread that reply heartbeat signals from the client
        client_heartbeat_socket, client_heartbeat_address = self._create_heartbeat_server(
        )
        self.client_thread = threading.Thread(
            target=self._reply_client_heartbeat,
            args=(client_heartbeat_socket, ))
        self.client_thread.setDaemon(True)

        # sends job information to the worker
        initialized_job = InitializedJob(
            self.job_address, worker_heartbeat_address,
            client_heartbeat_address, self.ping_heartbeat_address, None,
            os.getpid())
        self.job_socket.send_multipart(
            [remote_constants.NORMAL_TAG,
             cloudpickle.dumps(initialized_job)])
        _ = self.job_socket.recv_multipart()

    def _reply_ping(self, socket):
        """Create a socket server that reply the ping signal from client.
        This signal is used to make sure that the job is still alive.
        """
        while self.job_is_alive:
            message = socket.recv_multipart()
            socket.send_multipart([remote_constants.HEARTBEAT_TAG])
        socket.close(0)

    def _create_heartbeat_server(self, timeout=True):
        """Create a socket server that will raises timeout exception.
        """
        heartbeat_socket = self.ctx.socket(zmq.REP)
        if timeout:
            heartbeat_socket.setsockopt(
                zmq.RCVTIMEO, remote_constants.HEARTBEAT_RCVTIMEO_S * 1000)
        heartbeat_socket.linger = 0
        heartbeat_port = heartbeat_socket.bind_to_random_port(addr="tcp://*")
        heartbeat_address = "{}:{}".format(self.job_ip, heartbeat_port)
        return heartbeat_socket, heartbeat_address

    def _reply_client_heartbeat(self, socket):
        """Create a socket that replies heartbeat signals from the client.
        If the client has exited, the job will not exit, but reinitialized itself ,
        and let the master know that it is available for the new task. 
        """
        socket.setsockopt(zmq.RCVTIMEO, 5 * 1000)  # 5 seconds
        self.client_is_alive = True
        while self.client_is_alive:
            try:
                message = socket.recv_multipart()
                socket.send_multipart([remote_constants.HEARTBEAT_TAG])

            except zmq.error.Again as e:
                logger.warning(
                    "[Job] Cannot connect to the client. I am going to reset myself"
                )
                self.client_is_alive = False
        socket.close(0)

    def _reply_worker_heartbeat(self, socket):
        """create a socket that replies heartbeat signals from the worker.
        If the worker has exited, the job will exit automatically.
        """

        # a flag to decide when to exit heartbeat loop
        self.worker_is_alive = True
        while self.worker_is_alive and self.job_is_alive:
            try:
                message = socket.recv_multipart()
                socket.send_multipart([remote_constants.HEARTBEAT_TAG])

            except zmq.error.Again as e:
                logger.warning("[Job] Cannot connect to the worker{}. ".format(
                    self.worker_address) + "Job will quit.")
                self.worker_is_alive = False
                self.job_is_alive = False
        socket.close(0)

    def wait_for_files(self):
        """Wait for python files from remote object.

        When a remote object receives the allocated job address, it will send
        the python files to the job. Later, the job will save these files to a
        temporary directory and add the temporary diretory to Python's working
        directory.

        Returns:
            A temporary directory containing the python files.
        """

        while True:
            message = self.reply_socket.recv_multipart()
            tag = message[0]
            if tag == remote_constants.SEND_FILE_TAG:
                pyfiles = pickle.loads(message[1])
                envdir = tempfile.mkdtemp()
                for file in pyfiles:
                    code = pyfiles[file]
                    file = os.path.join(envdir, file)
                    with open(file, 'wb') as code_file:
                        code_file.write(code)
                self.reply_socket.send_multipart([remote_constants.NORMAL_TAG])
                return envdir
            else:
                logger.error(
                    "NotImplementedError:{}, received message:{}".format(
                        self.job_address, message))
                raise NotImplementedError

    def wait_for_connection(self):
        """Wait for connection from the remote object.

        The remote object will send its class information and initialization
        arguments to the job, these parameters are then used to create a
        local instance in the job process.

        Returns:
            A local instance of the remote class object.
        """

        message = self.reply_socket.recv_multipart()
        tag = message[0]
        obj = None
        if tag == remote_constants.INIT_OBJECT_TAG:
            cls = cloudpickle.loads(message[1])
            args, kwargs = cloudpickle.loads(message[2])

            try:
                obj = cls(*args, **kwargs)
            except Exception as e:
                traceback_str = str(traceback.format_exc())
                error_str = str(e)
                logger.error("traceback:\n{}".format(traceback_str))
                self.reply_socket.send_multipart([
                    remote_constants.EXCEPTION_TAG,
                    to_byte(error_str + "\ntraceback:\n" + traceback_str)
                ])
                self.client_is_alive = False
                return None

            self.reply_socket.send_multipart([remote_constants.NORMAL_TAG])
        else:
            logger.error("Message from job {}".format(message))
            self.reply_socket.send_multipart([
                remote_constants.EXCEPTION_TAG,
                b"[job]Unkonwn tag when tried to receive the class definition"
            ])
            raise NotImplementedError

        return obj

    def run(self):
        """An infinite loop waiting for a new task.
        """
        while self.job_is_alive:
            # receive files
            envdir = self.wait_for_files()
            previous_path = sys.path
            sys.path.append(envdir)
            self.client_thread.start()

            try:
                obj = self.wait_for_connection()
                assert obj is not None
                self.single_task(obj)
            except Exception as e:
                logger.error(
                    "Error occurs when running a single task. We will reset this job. Reason:{}"
                    .format(e))

            logger.warning("Restting the job")

            #restore the environmental variable
            sys.path = previous_path

            self.client_thread.join()
            client_heartbeat_socket, client_heartbeat_address = self._create_heartbeat_server(
            )
            self.client_thread = threading.Thread(
                target=self._reply_client_heartbeat,
                args=(client_heartbeat_socket, ))
            self.client_thread.setDaemon(True)
            initialized_job = InitializedJob(
                self.job_address,
                worker_heartbeat_address=None,
                client_heartbeat_address=client_heartbeat_address,
                ping_heartbeat_address=self.ping_heartbeat_address,
                worker_address=None,
                pid=None)
            self.master_socket.send_multipart([
                remote_constants.RESET_JOB_TAG,
                cloudpickle.dumps(initialized_job)
            ])
            self.master_socket.recv_multipart()

    def single_task(self, obj):
        """An infinite loop waiting for commands from the remote object.

        Each job will receive two kinds of message from the remote object:

        1. When the remote object calls a function, job will run the
           function on the local instance and return the results to the
           remote object.
        2. When the remote object is deleted, the job will quit and release
           related computation resources.
        """

        while self.job_is_alive and self.client_is_alive:
            message = self.reply_socket.recv_multipart()
            tag = message[0]

            if tag == remote_constants.CALL_TAG:
                assert obj is not None
                try:
                    function_name = to_str(message[1])
                    data = message[2]
                    args, kwargs = loads_argument(data)
                    ret = getattr(obj, function_name)(*args, **kwargs)
                    ret = dumps_return(ret)

                    self.reply_socket.send_multipart(
                        [remote_constants.NORMAL_TAG, ret])

                except Exception as e:
                    # reset the job
                    self.client_is_alive = False

                    error_str = str(e)
                    logger.error(error_str)

                    if type(e) == AttributeError:
                        self.reply_socket.send_multipart([
                            remote_constants.ATTRIBUTE_EXCEPTION_TAG,
                            to_byte(error_str)
                        ])

                    elif type(e) == SerializeError:
                        self.reply_socket.send_multipart([
                            remote_constants.SERIALIZE_EXCEPTION_TAG,
                            to_byte(error_str)
                        ])

                    elif type(e) == DeserializeError:
                        self.reply_socket.send_multipart([
                            remote_constants.DESERIALIZE_EXCEPTION_TAG,
                            to_byte(error_str)
                        ])

                    else:
                        traceback_str = str(traceback.format_exc())
                        logger.error("traceback:\n{}".format(traceback_str))
                        self.reply_socket.send_multipart([
                            remote_constants.EXCEPTION_TAG,
                            to_byte(error_str + "\ntraceback:\n" +
                                    traceback_str)
                        ])

            # receive DELETE_TAG from actor, and stop replying worker heartbeat
            elif tag == remote_constants.KILLJOB_TAG:
                self.reply_socket.send_multipart([remote_constants.NORMAL_TAG])
                self.client_is_alive = False
                logger.warning("An actor exits and will reset job {}.".format(
                    self.job_address))
            else:
                logger.error("Job message: {}".format(message))
                raise NotImplementedError


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--worker_address", required=True, type=str, help="worker_address")
    parser.add_argument(
        "--master_address", required=True, type=str, help="master_address")
    args = parser.parse_args()
    job = Job(args.worker_address, args.master_address)
    job.run()
