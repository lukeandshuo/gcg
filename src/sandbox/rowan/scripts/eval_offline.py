import argparse
import glob
import os
import yaml

import numpy as np

from gcg.data.logger import logger
from gcg.envs.env_utils import create_env
from gcg.data import mypickle
from gcg.sampler.replay_pool import ReplayPool

from gcg.policies.gcg_policy import GCGPolicy

from bnn_plotter import BnnPlotter

class EvalOffline(object):
    def __init__(self, yaml_path):
        with open(yaml_path, 'r') as f:
            self._params = yaml.load(f)

        logger.setup(display_name=self._params['exp_name'],
                     log_path=os.path.join(self._save_dir, 'log.txt'),
                     lvl=self._params['log_level'])

        logger.info('Yaml {0}'.format(yaml_path))

        logger.info('')
        logger.info('Creating environment')
        self._env = create_env(self._params['alg']['env'])

        logger.info('')
        logger.info('Creating model')
        self._model = self._create_model()

        logger.info('')
        logger.info('Loading data')
        self._replay_pool = self._load_data(self._data_file_name)
        logger.info('Size of replay pool: {0:d}'.format(len(self._replay_pool)))
        self._replay_holdout_pool = self._load_data(self._data_holdout_file_name)
        logger.info('Size of holdout replay pool: {0:d}'.format(len(self._replay_holdout_pool)))

        # TODO: load specific parts
        if self._init_checkpoint_file_name is not None:
            logger.info('')
            logger.info('Loading checkpoint {0} for {1}'.format(self._init_checkpoint_file_name,
                                                                self._params['offline']['init_restore']))
            self._model.restore(self._init_checkpoint_file_name,
                                train_restore=self._params['offline']['init_restore'])

        self._restore_train_policy()

    #############
    ### Files ###
    #############

    @property
    def _dir(self):
        file_dir = os.path.realpath(os.path.dirname(__file__))
        dir = os.path.join(file_dir[:file_dir.find('gcg/src')], 'gcg/data')
        assert (os.path.exists(dir))
        return dir

    @property
    def _data_file_name(self):
        return os.path.join(self._dir, self._params['offline']['data'])

    @property
    def _data_holdout_file_name(self):
        return os.path.join(self._dir, self._params['offline']['data_holdout'])

    @property
    def _save_dir(self):
        dir = os.path.join(self._dir, self._params['exp_name'])
        os.makedirs(dir, exist_ok=True)
        return dir

    def _train_policy_file_name(self, itr):
        return os.path.join(self._save_dir, 'itr_{0:04d}_train_policy.ckpt'.format(itr))

    @property
    def _init_checkpoint_file_name(self):
        if self._params['offline']['init_checkpoint'] is not None:
            return os.path.join(self._dir, self._params['offline']['init_checkpoint'])

    #############
    ### Model ###
    #############

    def _create_model(self):
        policy_class = self._params['policy']['class']
        PolicyClass = eval(policy_class)
        policy_params = self._params['policy'][policy_class]

        policy = PolicyClass(
            env_spec=self._env.spec,
            exploration_strategies={},
            **policy_params,
            **self._params['policy']
        )

        return policy

    def _restore_train_policy(self):
        """
        :return: iteration that it is currently on
        """
        itr = 0
        while len(glob.glob(self._train_policy_file_name(itr) + '*')) > 0:
            itr += 1

        if itr > 0:
            logger.info('Loading train policy from iteration {0}...'.format(itr - 1))
            self._policy.restore(self._train_policy_file_name(itr - 1), train=True)
            logger.info('Loaded train policy!')

    def _save_train_policy(self, save_itr):
        self._model.save(self._train_policy_file_name(save_itr), train=True)

    ############
    ### Data ###
    ############

    def _load_data(self, folder):
        """
        Loads all .pkl files that can be found recursively from this folder
        """
        assert(os.path.exists(folder))

        rollouts = []
        num_load_success, num_load_fail = 0, 0
        for fname in glob.iglob('{0}/**/*.pkl'.format(folder), recursive=True):
            try:
                rollouts += mypickle.load(fname)['rollouts']
                num_load_success += 1
            except:
                num_load_fail += 1
        logger.info('Files successfully loaded: {0:.2f}%'.format(100. * num_load_success /
                                                                 float(num_load_success + num_load_fail)))

        replay_pool = ReplayPool(
            env_spec=self._env.spec,
            env_horizon=self._env.horizon,
            N=self._model.N,
            gamma=self._model.gamma,
            size=int(1.1 * sum([len(r['dones']) for r in rollouts])),
            obs_history_len=self._model.obs_history_len,
            sampling_method='uniform',
            save_rollouts=False,
            save_rollouts_observations=False,
            save_env_infos=False,
            replay_pool_params={}
        )

        curr_len = 0
        for rollout in rollouts:
            replay_pool.store_rollout(curr_len, rollout)
            curr_len += len(rollout['dones'])

        return replay_pool

    #############
    ### Train ###
    #############

    def train(self):
        logger.info('Training model')

        alg_args = self._params['alg']
        total_steps = int(alg_args['total_steps'])
        save_every_n_steps = int(alg_args['save_every_n_steps'])
        update_target_after_n_steps = int(alg_args['update_target_after_n_steps'])
        update_target_every_n_steps = int(alg_args['update_target_every_n_steps'])
        log_every_n_steps = int(alg_args['log_every_n_steps'])
        batch_size = alg_args['batch_size']

        save_itr = 0
        for step in range(total_steps):
            batch = self._replay_pool.sample(batch_size)
            self._model.train_step(step, *batch, use_target=True)

            ### update target network
            if step > update_target_after_n_steps and step % update_target_every_n_steps == 0:
                self._model.update_target()

            ### log
            if step > 0 and step % log_every_n_steps == 0:
                logger.record_tabular('Step', step)
                self._model.log()
                logger.dump_tabular(print_func=logger.info)

            ### save model
            if step > 0 and step % save_every_n_steps == 0:
                logger.info('Saving files for itr {0}'.format(save_itr))
                self._save_train_policy(save_itr)
                save_itr += 1

        ### always save the end
        self._save_train_policy(save_itr)

    ################
    ### Evaluate ###
    ################

    def evaluate(self, plotter, eval_on_holdout=False):
        logger.info('Evaluating model')

        if eval_on_holdout:
            replay_pool = self._replay_holdout_pool
        else:
            replay_pool = self._replay_pool

        # get collision idx in obs_vec
        vec_spec = self._env.observation_vec_spec
        obs_vec_start_idxs = np.cumsum([space.flat_dim for space in vec_spec.values()]) - 1
        coll_idx = obs_vec_start_idxs[list(vec_spec.keys()).index('coll')]

        ### sample from the data, get the outputs
        sample_size = 200
        steps, (observations_im, observations_vec), actions, rewards, values, dones, logprobs = replay_pool.sample(sample_size)
        observations = (observations_im[:, :self._model.obs_history_len, :],
                        observations_vec[:, :self._model.obs_history_len, :])
        labels = (np.cumsum(observations_vec[:, self._model.obs_history_len:, coll_idx], axis=1) >= 1.).astype(float)

        num_bnn_samples = 1000
        preds = []
        for _ in range(num_bnn_samples):
            yhats, bhats = self._model.get_model_outputs(observations, actions)
            preds.append(yhats['coll'])
        preds = np.asarray(preds)

        plotter(preds, labels)
        # import IPython; IPython.embed()
        # BnnPlotter.plot_dropout(outputs, rewards)
        # BnnPlotter.plot_predtruth(outputs, rewards)
        # BnnPlotter.plot_hist(outputs, rewards)
        # BnnPlotter.plot_hist_no_time_structure(outputs, rewards)
        # BnnPlotter.plot_roc(outputs, rewards)
        # BnnPlotter.plot_scatter_prob_and_sigma(outputs, rewards)

        # import pickle
        # file_outputs = open("outputs.pkl", 'wb')
        # file_rewards = open("rewards.pkl", 'wb')
        # pickle.dump(outputs, file_outputs)
        # pickle.dump(rewards, file_rewards)

        # file_outputs = open("outputs.pkl", 'rb')
        # file_outputs.seek(0)
        # file_rewards = open("rewards.pkl", 'rb')
        # file_rewards.seek(0)
        # outputs = pickle.load(file_outputs)
        # rewards = pickle.load(file_rewards)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('yaml', type=str, help='yaml file with all parameters')
    parser.add_argument('--no_train', action='store_true', help='do not train model on data')
    parser.add_argument('--no_eval', action='store_true', help='do not evaluate model')
    args = parser.parse_args()

    curr_dir = os.path.realpath(os.path.dirname(__file__))
    yaml_dir = os.path.join(curr_dir[:curr_dir.find('gcg/src')], 'gcg/yamls')
    assert (os.path.exists(yaml_dir))
    yaml_path = os.path.join(yaml_dir, args.yaml)

    model = EvalOffline(yaml_path)
    if not args.no_train:
        model.train()

    if not args.no_eval:
        model.evaluate(BnnPlotter.plot_dropout, eval_on_holdout=False)
        model.evaluate(BnnPlotter.plot_dropout, eval_on_holdout=True)