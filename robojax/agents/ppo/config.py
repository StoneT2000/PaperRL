"""
Configurations and utility classes
"""

import dataclasses
from typing import Optional

import chex
from flax import struct


@dataclasses.dataclass
class PPOConfig:
    """
    Configuration datalcass for PPO
    """
    normalize_advantage: Optional[bool] = True
    gamma: Optional[float] = 0.99
    gae_lambda: Optional[float] = 0.97
    clip_ratio: Optional[float] = 0.2
    ent_coef: Optional[float] = 0.0
    pi_coef: Optional[float] = 1.0
    vf_coef: Optional[float] = 1.0
    dapg_lambda: Optional[float] = 0.1
    dapg_damping: Optional[float] = 0.99
    target_kl: Optional[float] = 0.01


@struct.dataclass
class TimeStep:
    log_p: chex.Array
    action: chex.Array
    env_obs: chex.Array
    adv: chex.Array
    reward: chex.Array
    ret: chex.Array
    value: chex.Array
    done: chex.Array
    ep_len: chex.Array
