from functools import partial
from typing import Any, Callable, Tuple

import chex
import distrax
import gym
import jax
import jax.numpy as jnp
import numpy as np
from flax import struct

from robojax.agents.ppo.config import PPOConfig, TimeStep
from robojax.agents.ppo.loss import actor_loss_fn, critic_loss_fn
from robojax.loop.loop import GymLoop, JaxLoop
from robojax.loop.sampler import BufferSampler
from robojax.models.ac.core import ActorCritic
from robojax.models.model import Model, Params

PRNGKey = chex.PRNGKey


@partial(jax.jit, static_argnames=["gamma", "gae_lambda"])
@partial(jax.vmap, in_axes=(1, 1, 1, None, None), out_axes=1)
def gae_advantages(rewards, dones, values, gamma: float, gae_lambda: float):
    N = len(rewards)
    advantages = jnp.zeros((N + 1))
    not_dones = ~dones

    value_diffs = gamma * values[1:] * not_dones - values[:-1]
    deltas = rewards + value_diffs

    def body_fun(gae, t):
        gae = deltas[t] + gamma * gae_lambda * not_dones[t] * gae
        return gae, gae

    indices = jnp.arange(N)[::-1]
    _, advantages = jax.lax.scan(
        body_fun,
        0.0,
        indices,
    )

    advantages = advantages[::-1]
    return jax.lax.stop_gradient(advantages)


class PPO:
    def __init__(self, env: gym.Env, jax_env: bool, cfg: PPOConfig = {}) -> None:
        self.jax_env = jax_env

        # if env.step.__class__.__name__ == "CompiledFunction":
        # self.jax = True
        self.cfg = PPOConfig(**cfg)
        self.env = env
        if self.jax_env:

            def rollout_callback(
                action, env_obs, reward, ep_ret, ep_len, next_env_obs, done, info, aux
            ):
                return TimeStep(
                    action=action,
                    env_obs=env_obs,
                    reward=reward,
                    adv=0,
                    log_p=aux["log_p"],
                    ret=ep_ret,
                    value=aux["value"],
                    done=done,
                    ep_len=ep_len,
                )

            self.loop = JaxLoop(
                self.env.reset, self.env.step, rollout_callback=rollout_callback
            )
            self.train_step = jax.jit(
                self.train_step,
                static_argnames=[
                    "self",
                    "update_iters",
                    "rollout_steps_per_env",
                    "num_envs",
                    "apply_fn",
                    "batch_size",
                ],
            )
        else:
            # we expect env to be a vectorized env now
            def rollout_callback(
                action, env_obs, reward, ep_ret, ep_len, next_env_obs, done, info, aux
            ):
                batch_size = len(env_obs)
                return dict(
                    action=action,
                    env_obs=env_obs,
                    reward=reward,
                    adv=jnp.zeros((batch_size)),
                    log_p=aux["log_p"],
                    ret=ep_ret,
                    value=aux["value"],
                    done=done,
                    ep_len=jnp.array(ep_len),
                )

            self.loop = GymLoop(env, rollout_callback=rollout_callback)

    def train(
        self,
        update_iters: int,
        steps_per_epoch: int,
        num_envs: int,
        rng_key: PRNGKey,
        epochs: int,
        ac: ActorCritic,
        batch_size: int,
    ):

        if self.jax_env:

            def apply_fn(rng_key, actor, critic, obs):
                res = ac.step(rng_key, actor, critic, obs)
                return res

        else:

            def apply_fn(rng_key, actor, critic, obs):
                res = ac.step(rng_key, actor, critic, obs)
                return np.array(res[0]), res[1]

        for t in range(epochs):
            rng_key, train_rng_key = jax.random.split(rng_key)
            print(f"=== {t} ===")
            actor, critic, aux = self.train_step(
                rng_key=train_rng_key,
                update_iters=update_iters,
                rollout_steps_per_env=steps_per_epoch,
                num_envs=num_envs,
                actor=ac.actor,
                critic=ac.critic,
                apply_fn=apply_fn,
                batch_size=batch_size,
            )
            ac.actor = actor
            ac.critic = critic

            buffer = aux["buffer"]
            ep_lens = np.asarray(buffer.ep_len)
            ep_rets = np.asarray(buffer.ret)
            episode_ends = np.asarray(buffer.done) == True
            info = dict(
                ep_lens=ep_lens[episode_ends].mean(),
                ep_rets=ep_rets[episode_ends].mean(),
            )
            print(info)
            # import ipdb;ipdb.set_trace()

    def train_step(
        self,
        rng_key: PRNGKey,
        update_iters: int,
        rollout_steps_per_env: int,
        num_envs: int,
        actor: Model,
        critic: Model,  # steps per env
        apply_fn: Callable,  # rollout function
        batch_size: int,
    ):
        rng_key, buffer_rng_key = jax.random.split(rng_key)

        def apply_fn_wrapped(rng_key, obs):
            return apply_fn(rng_key, actor, critic, obs)

        buffer, info = self.collect_buffer(
            rng_key=buffer_rng_key,
            rollout_steps_per_env=rollout_steps_per_env,
            num_envs=num_envs,
            apply_fn=apply_fn_wrapped,
        )

        actor, critic, update_aux = self.update_parameters(
            rng_key=rng_key,
            actor=actor,
            critic=critic,
            update_actor=True,
            update_critic=True,
            update_iters=update_iters,
            num_envs=num_envs,
            batch_size=batch_size,
            buffer=buffer,
        )
        return actor, critic, dict(buffer=buffer, update_aux=update_aux)

    @partial(
        jax.jit,
        static_argnames=[
            "self",
            "update_actor",
            "update_critic",
            "update_iters",
            "num_envs",
            "batch_size",
        ],
    )
    def update_parameters(
        self,
        rng_key: PRNGKey,
        actor: Model,
        critic: Model,
        update_actor: bool,
        update_critic: bool,
        update_iters: int,
        num_envs: int,
        batch_size: int,
        buffer: TimeStep,
    ):
        sampler = BufferSampler(
            buffer, buffer_size=buffer.action.shape[0], num_envs=num_envs
        )

        def update_step_fn(data: Tuple[PRNGKey, Model, Model], unused):
            rng_key, actor, critic = data
            rng_key, sample_rng_key = jax.random.split(rng_key)
            batch = TimeStep(**sampler.sample_random_batch(sample_rng_key, batch_size))
            info_a, info_c = None, None
            new_actor = actor
            new_critic = critic
            if update_actor:
                grads_a_fn = jax.grad(
                    actor_loss_fn(
                        clip_ratio=self.cfg.clip_ratio,
                        actor_apply_fn=actor.apply_fn,
                        batch=batch,
                    ),
                    has_aux=True,
                )
                grads, info_a = grads_a_fn(actor.params)
                new_actor = actor.apply_gradients(grads=grads)
            if update_critic:
                grads_c_fn = jax.grad(
                    critic_loss_fn(critic_apply_fn=critic.apply_fn, batch=batch),
                    has_aux=True,
                )
                grads, info_c = grads_c_fn(critic.params)
                new_critic = critic.apply_gradients(grads=grads)
            return (rng_key, new_actor, new_critic), (info_a, info_c)

        update_init = (rng_key, actor, critic)
        carry, update_aux = jax.lax.scan(
            update_step_fn, update_init, (), length=update_iters
        )
        _, actor, critic = carry

        return actor, critic, update_aux

    def collect_buffer(
        self, rng_key, rollout_steps_per_env: int, num_envs: int, apply_fn: Callable
    ):
        # buffer collection is not jitted if env is not jittable
        # regardless this function returns a struct.dataclass object with all the data in jax.numpy arrays for use

        rng_key, *env_rng_keys = jax.random.split(rng_key, num_envs + 1)
        buffer: TimeStep = self.loop.rollout(
            env_rng_keys,
            apply_fn,
            rollout_steps_per_env + 1,  # extra 1 for final value computation
        )

        if not self.jax_env:
            # if not a jax based env, then buffer is a python dictionary and we convert it
            buffer = TimeStep(**buffer)

        # rest of the code here is jitted and / or vmapped
        advantages = gae_advantages(
            buffer.reward[:-1],
            buffer.done[:-1],
            buffer.value,
            self.cfg.gamma,
            self.cfg.gae_lambda,
        )
        if self.cfg.normalize_advantage:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
        returns = advantages + buffer.value[-1, :]
        buffer = buffer.replace(
            adv=advantages,
            ret=returns,
            log_p=buffer.log_p[:-1],
            action=buffer.action[:-1],
            env_obs=buffer.env_obs[:-1],
            done=buffer.done[:-1],
            ep_len=buffer.ep_len[:-1],
        )

        return buffer, {}
