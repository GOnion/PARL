#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import warnings
warnings.simplefilter('default')

from parl.core.fluid import layers
from copy import deepcopy
from paddle import fluid
from parl.core.fluid.algorithm import Algorithm
from parl.utils.deprecation import deprecated

__all__ = ['DDPG']


class DDPG(Algorithm):
    def __init__(self,
                 model,
                 hyperparas=None,
                 gamma=None,
                 tau=None,
                 actor_lr=None,
                 critic_lr=None):
        """  DDPG algorithm
        
        Args:
            model (parl.Model): forward network of actor and critic.
                                The function get_actor_params() of model should be implemented.
            hyperparas (dict): (deprecated) dict of hyper parameters.
            gamma (float): discounted factor for reward computation.
            tau (float): decay coefficient when updating the weights of self.target_model with self.model
            actor_lr (float): learning rate of the actor model
            critic_lr (float): learning rate of the critic model
        """
        if hyperparas is not None:
            warnings.warn(
                "the `hyperparas` argument of `__init__` function in `parl.Algorithms.DDPG` is deprecated since version 1.2 and will be removed in version 1.3.",
                DeprecationWarning,
                stacklevel=2)
            self.gamma = hyperparas['gamma']
            self.tau = hyperparas['tau']
            self.actor_lr = hyperparas['actor_lr']
            self.critic_lr = hyperparas['critic_lr']
        else:
            assert isinstance(gamma, float)
            assert isinstance(tau, float)
            assert isinstance(actor_lr, float)
            assert isinstance(critic_lr, float)
            self.gamma = gamma
            self.tau = tau
            self.actor_lr = actor_lr
            self.critic_lr = critic_lr

        self.model = model
        self.target_model = deepcopy(model)

    @deprecated(
        deprecated_in='1.2', removed_in='1.3', replace_function='predict')
    def define_predict(self, obs):
        """ use actor model of self.model to predict the action
        """
        return self.predict(obs)

    def predict(self, obs):
        """ use actor model of self.model to predict the action
        """
        return self.model.policy(obs)

    @deprecated(
        deprecated_in='1.2', removed_in='1.3', replace_function='learn')
    def define_learn(self, obs, action, reward, next_obs, terminal):
        """ update actor and critic model with DDPG algorithm
        """
        return self.learn(obs, action, reward, next_obs, terminal)

    def learn(self, obs, action, reward, next_obs, terminal):
        """ update actor and critic model with DDPG algorithm
        """
        actor_cost = self._actor_learn(obs)
        critic_cost = self._critic_learn(obs, action, reward, next_obs,
                                         terminal)
        return actor_cost, critic_cost

    def _actor_learn(self, obs):
        action = self.model.policy(obs)
        Q = self.model.value(obs, action)
        cost = layers.reduce_mean(-1.0 * Q)
        optimizer = fluid.optimizer.AdamOptimizer(self.actor_lr)
        optimizer.minimize(cost, parameter_list=self.model.get_actor_params())
        return cost

    def _critic_learn(self, obs, action, reward, next_obs, terminal):
        next_action = self.target_model.policy(next_obs)
        next_Q = self.target_model.value(next_obs, next_action)

        terminal = layers.cast(terminal, dtype='float32')
        target_Q = reward + (1.0 - terminal) * self.gamma * next_Q
        target_Q.stop_gradient = True

        Q = self.model.value(obs, action)
        cost = layers.square_error_cost(Q, target_Q)
        cost = layers.reduce_mean(cost)
        optimizer = fluid.optimizer.AdamOptimizer(self.critic_lr)
        optimizer.minimize(cost)
        return cost

    def sync_target(self, gpu_id=None, decay=None):
        if gpu_id is not None:
            warnings.warn(
                "the `gpu_id` argument of `sync_target` function in `parl.Algorithms.DDPG` is deprecated since version 1.2 and will be removed in version 1.3.",
                DeprecationWarning,
                stacklevel=2)
        if decay is None:
            decay = 1.0 - self.tau
        self.model.sync_weights_to(self.target_model, decay=decay)
