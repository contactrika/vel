import argparse

import torch
import torch.optim

from vel.api import TrainingInfo, EpochInfo
from vel.modules.input.normalize_observations import NormalizeObservationsFactory
from vel.rl.buffers.circular_replay_buffer import CircularReplayBuffer
from vel.rl.env_roller.transition_replay_env_roller import TransitionReplayEnvRoller
from vel.rl.metrics import EpisodeRewardMetric
from vel.rl.modules.noise.ou_noise import OuNoise
from vel.storage.streaming.stdout import StdoutStreaming
from vel.util.random import set_seed
from vel.rl.env.mujoco import MujocoEnv
from vel.rl.models.deterministic_policy_model import DeterministicPolicyModelFactory
from vel.rl.models.backbone.mlp import MLPFactory
from vel.rl.reinforcers.buffered_off_policy_iteration_reinforcer import (
    BufferedOffPolicyIterationReinforcer, BufferedOffPolicyIterationReinforcerSettings
)
from vel.rl.algo.policy_gradient.ddpg import DeepDeterministicPolicyGradient
from vel.rl.vecenv.dummy import DummyVecEnvWrapper
from vel.optimizers.adam import AdamFactory

from vel.rl.models.stochastic_policy_model import StochasticPolicyModelFactory
from vel.rl.reinforcers.on_policy_iteration_reinforcer import (
    OnPolicyIterationReinforcer, OnPolicyIterationReinforcerSettings
)
from vel.rl.algo.policy_gradient.ppo import PpoPolicyGradient
from vel.rl.env_roller.step_env_roller import StepEnvRoller
from vel.schedules.linear import LinearSchedule


def get_ddpg(vec_env, device):
    model_factory = DeterministicPolicyModelFactory(
        input_block=NormalizeObservationsFactory(input_shape=17),
        policy_backbone=MLPFactory(input_length=17, hidden_layers=[64, 64], activation='tanh'),
        value_backbone=MLPFactory(input_length=23, hidden_layers=[64, 64], activation='tanh'),
    )
    model = model_factory.instantiate(action_space=vec_env.action_space)
    reinforcer = BufferedOffPolicyIterationReinforcer(
        device=device,
        environment=vec_env,
        settings=BufferedOffPolicyIterationReinforcerSettings(
            rollout_steps=2,
            training_steps=64,
        ),
        model=model,
        algo=DeepDeterministicPolicyGradient(
            model_factory=model_factory,
            discount_factor=0.99,
            tau=0.01,
        ),
        env_roller=TransitionReplayEnvRoller(
            environment=vec_env,
            device=device,
            action_noise=OuNoise(std_dev=0.2, environment=vec_env),
            replay_buffer=CircularReplayBuffer(
                buffer_capacity=1_000_000,
                buffer_initial_size=2_000,
                num_envs=vec_env.num_envs,
                observation_space=vec_env.observation_space,
                action_space=vec_env.action_space
            ),
            normalize_returns=True,
            discount_factor=0.99
        ),
    )
    return model, reinforcer


def get_ppo(vec_env, device):
    model = StochasticPolicyModelFactory(
        input_block=NormalizeObservationsFactory(input_shape=17),
        backbone=MLPFactory(input_length=23, hidden_layers=[64, 64], activation='tanh'),
    ).instantiate(action_space=vec_env.action_space)
    cliprange = LinearSchedule(
        initial_value=0.1,
        final_value=0.0
    )
    reinforcer = OnPolicyIterationReinforcer(
        device=device,
        settings=OnPolicyIterationReinforcerSettings(
            batch_size=256,
            experience_replay=4,
            number_of_steps=128
        ),
        model=model,
        algo=PpoPolicyGradient(
            entropy_coefficient=0.01,
            value_coefficient=0.5,
            max_grad_norm=0.5,
            discount_factor=0.99,
            gae_lambda=0.95,
            cliprange=cliprange
        ),
        env_roller=StepEnvRoller(
            environment=vec_env,
            device=device,
        )
    )
    return model, reinforcer



def pivoting_rl(args):
    device = torch.device('cuda:'+str(args.gpu) if torch.cuda.is_available() else 'cpu')
    seed = 1002

    # Set random seed in python std lib, numpy and pytorch
    set_seed(seed)

    vec_env = DummyVecEnvWrapper(
        MujocoEnv('HalfCheetah-v2')
    ).instantiate(parallel_envs=1, seed=seed)

    if args.algo == 'ddpg':
       model, reinforcer = get_ddpg(vec_env, device)
    elif args.algo == 'ppo':
        model, reinforcer = get_ppo(vec_env, device)
    else:
        print('Unknown algo', args.algo); assert(False)


    # Optimizer helper - A weird regularization settings I've copied from OpenAI code
    adam_optimizer = AdamFactory(
        lr=[1.0e-4, 1.0e-3, 1.0e-3],
        weight_decay=[0.0, 0.0, 0.001],
        eps=1.0e-4,
        layer_groups=True
    ).instantiate(model)

    # Overall information store for training information
    training_info = TrainingInfo(
        metrics=[
            EpisodeRewardMetric('episode_rewards'),  # Calculate average reward from episode
        ],
        callbacks=[StdoutStreaming()]  # Print live metrics every epoch to standard output
    )

    # A bit of training initialization bookkeeping...
    training_info.initialize()
    reinforcer.initialize_training(training_info)
    training_info.on_train_begin()

    # Let's make 20 batches per epoch to average metrics nicely
    num_epochs = int(1.0e6 / 2 / 1000)

    # Normal handrolled training loop
    for i in range(1, num_epochs+1):
        epoch_info = EpochInfo(
            training_info=training_info,
            global_epoch_idx=i,
            batches_per_epoch=1000,
            optimizer=adam_optimizer
        )

        reinforcer.train_epoch(epoch_info)

    training_info.on_train_end()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Pivoting")
    parser.add_argument('--gpu', type=int, default=0, help='GPU id')
    parser.add_argument('--algo', type=str, default='ddpg', help='RL algo')
    args = parser.parse_args()
    pivoting_rl(args)
