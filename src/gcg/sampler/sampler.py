import itertools
import pickle

import numpy as np

from gcg.envs.spaces.discrete import Discrete
from gcg.envs.env_utils import create_env
from gcg.envs.spaces.box import Box
from gcg.envs.vec_env_executor import VecEnvExecutor
from gcg.sampler.replay_pool import ReplayPool
from gcg.data import mypickle


class Sampler(object):
    def __init__(self, policy, env, n_envs, replay_pool_size, max_path_length, sampling_method,
                 save_rollouts=False, save_rollouts_observations=True, save_env_infos=False, env_str=None, replay_pool_params={}):
        self._policy = policy
        self._n_envs = n_envs

        assert(self._n_envs == 1) # b/c policy reset

        self._replay_pools = [ReplayPool(env.spec,
                                         env.horizon,
                                         policy.N,
                                         policy.gamma,
                                         replay_pool_size // n_envs,
                                         obs_history_len=policy.obs_history_len,
                                         sampling_method=sampling_method,
                                         save_rollouts=save_rollouts,
                                         save_rollouts_observations=save_rollouts_observations,
                                         save_env_infos=save_env_infos,
                                         replay_pool_params=replay_pool_params)
                              for _ in range(n_envs)]

        if self._n_envs == 1:
            envs = [env]
        else:
            try:
                envs = [pickle.loads(pickle.dumps(env)) for _ in range(self._n_envs)] if self._n_envs > 1 else [env]
            except:
                envs = [create_env(env_str) for _ in range(self._n_envs)] if self._n_envs > 1 else [env]
        ### need to seed each environment if it is GymEnv
        # TODO: set seed
        self._vec_env = VecEnvExecutor(
            envs=envs,
            max_path_length=max_path_length
        )

    @property
    def n_envs(self):
        return self._n_envs

    ##################
    ### Statistics ###
    ##################

    @property
    def statistics(self):
        return ReplayPool.statistics_pools(self._replay_pools)

    def __len__(self):
        return sum([len(rp) for rp in self._replay_pools])

    ####################
    ### Add to pools ###
    ####################

    def step(self, step, take_random_actions=False, explore=True):
        """ Takes one step in each simulator and adds to respective replay pools """
        ### store last observations and get encoded
        encoded_observations_im = []
        encoded_observations_vec = []
        for i, (replay_pool, observation) in enumerate(zip(self._replay_pools, self._curr_observations)):
            replay_pool.store_observation(step + i, observation)
            encoded_observation = replay_pool.encode_recent_observation()
            encoded_observations_im.append(encoded_observation[0])
            encoded_observations_vec.append(encoded_observation[1])

        ### get actions
        if take_random_actions:
            actions = [self._vec_env.action_space.sample() for _ in range(self._n_envs)]
            est_values = [np.nan] * self._n_envs
            if isinstance(self._vec_env.action_space, Discrete):
                logprobs = [-np.log(self._vec_env.action_space.flat_dim)] * self._n_envs
            elif isinstance(self._vec_env.action_space, Box):
                low = self._vec_env.action_space.low
                high = self._vec_env.action_space.high
                logprobs = [-np.sum(np.log(high - low))] * self._n_envs
            else:
                raise NotImplementedError
            action_infos = [{} for _ in range(self._n_envs)]
        else:
            actions, est_values, logprobs, action_infos = self._policy.get_actions(
                steps=list(range(step, step + self._n_envs)),
                current_episode_steps=self._vec_env.current_episode_steps,
                observations=(encoded_observations_im, encoded_observations_vec),
                explore=explore)

        ### take step
        next_observations, rewards, dones, env_infos = self._vec_env.step(actions)
        for env_info, action_info in zip(env_infos, action_infos):
            env_info.update(action_info)

        if np.any(dones):
            self._policy.reset_get_action()

        ### add to replay pool
        for replay_pool, action, reward, done, env_info, est_value, logprob in \
                zip(self._replay_pools, actions, rewards, dones, env_infos, est_values, logprobs):
            replay_pool.store_effect(action, reward, done, env_info, est_value, logprob)

        self._curr_observations = next_observations

    def trash_current_rollouts(self):
        """ In case an error happens """
        steps_removed = 0
        for replay_pool in self._replay_pools:
            steps_removed += replay_pool.trash_current_rollout()
        return steps_removed

    def reset(self):
        self._curr_observations = self._vec_env.reset()
        for replay_pool in self._replay_pools:
            replay_pool.force_done()

    ####################
    ### Add rollouts ###
    ####################

    def add_rollouts(self, rollout_filenames, max_to_add=None):
        step = sum([len(replay_pool) for replay_pool in self._replay_pools])
        itr = 0
        replay_pools = itertools.cycle(self._replay_pools)
        done_adding = False

        for fname in rollout_filenames:
            rollouts = mypickle.load(fname)['rollouts']
            itr += 1

            for rollout, replay_pool in zip(rollouts, replay_pools):
                r_len = len(rollout['dones'])
                if max_to_add is not None and step + r_len >= max_to_add:
                    diff = max_to_add - step
                    for k in ('observations', 'actions', 'rewards', 'dones', 'logprobs'):
                        rollout[k] = rollout[k][:diff]
                    done_adding = True
                    r_len = len(rollout['dones'])

                replay_pool.store_rollout(step, rollout)
                step += r_len

                if done_adding:
                    break

            if done_adding:
                break

    #########################
    ### Sample from pools ###
    #########################

    def can_sample(self):
        return np.any([replay_pool.can_sample() for replay_pool in self._replay_pools])

    def sample(self, batch_size):
        return ReplayPool.sample_pools(self._replay_pools, batch_size,
                                       only_completed_episodes=self._policy.only_completed_episodes)

    ###############
    ### Logging ###
    ###############

    def log(self, prefix=''):
        ReplayPool.log_pools(self._replay_pools, prefix=prefix)

    def get_recent_paths(self):
        return ReplayPool.get_recent_paths_pools(self._replay_pools)
