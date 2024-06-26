from typing import Tuple, Union

import chex
import jax
import jax.numpy as jnp

# These functions are generally taken from rlax but edited to explicitly take in a batch of data.
# This is because the original rlax functions are not batched and are meant to be used with vmap,
# which can be much slower.


def batch_truncated_generalized_advantage_estimation(
    r_t: chex.Array,
    discount_t: chex.Array,
    lambda_: Union[chex.Array, chex.Scalar],
    values: chex.Array,
    stop_target_gradients: bool = True,
    time_major: bool = False,
) -> Tuple[chex.Array, chex.Array]:
    """Computes truncated generalized advantage estimates for a sequence length k.

    The advantages are computed in a backwards fashion according to the equation:
    Âₜ = δₜ + (γλ) * δₜ₊₁ + ... + ... + (γλ)ᵏ⁻ᵗ⁺¹ * δₖ₋₁
    where δₜ = rₜ₊₁ + γₜ₊₁ * v(sₜ₊₁) - v(sₜ).

    See Proximal Policy Optimization Algorithms, Schulman et al.:
    https://arxiv.org/abs/1707.06347

    Note: This paper uses a different notation than the RLax standard
    convention that follows Sutton & Barto. We use rₜ₊₁ to denote the reward
    received after acting in state sₜ, while the PPO paper uses rₜ.

    Args:
        r_t: Sequence of rewards at times [1, k]
        discount_t: Sequence of discounts at times [1, k]
        lambda_: Mixing parameter; a scalar or sequence of lambda_t at times [1, k]
        values: Sequence of values under π at times [0, k]
        stop_target_gradients: bool indicating whether or not to apply stop gradient
        to targets.
        time_major: If True, the first dimension of the input tensors is the time
        dimension.

    Returns:
        Multistep truncated generalized advantage estimation at times [0, k-1].
        The target values at times [0, k-1] are also returned.
    """
    # Swap axes to make time axis the first dimension
    if not time_major:
        batch_size = r_t.shape[0]
        r_t, discount_t, values = jax.tree_map(
            lambda x: jnp.swapaxes(x, 0, 1), (r_t, discount_t, values)
        )
    else:
        batch_size = r_t.shape[1]

    chex.assert_type([r_t, values, discount_t], float)

    lambda_ = jnp.ones_like(discount_t) * lambda_  # If scalar, make into vector.

    delta_t = r_t + discount_t * values[1:] - values[:-1]

    # Iterate backwards to calculate advantages.
    def _body(
        acc: chex.Array, xs: Tuple[chex.Array, chex.Array, chex.Array]
    ) -> Tuple[chex.Array, chex.Array]:
        deltas, discounts, lambda_ = xs
        acc = deltas + discounts * lambda_ * acc
        return acc, acc

    _, advantage_t = jax.lax.scan(
        _body, jnp.zeros(batch_size), (delta_t, discount_t, lambda_), reverse=True, unroll=16
    )

    target_values = values[:-1] + advantage_t

    if not time_major:
        # Swap axes back to original shape
        advantage_t, target_values = jax.tree_map(
            lambda x: jnp.swapaxes(x, 0, 1), (advantage_t, target_values)
        )

    if stop_target_gradients:
        advantage_t, target_values = jax.tree_map(
            lambda x: jax.lax.stop_gradient(x), (advantage_t, target_values)
        )

    return advantage_t, target_values


def batch_n_step_bootstrapped_returns(
    r_t: chex.Array,
    discount_t: chex.Array,
    v_t: chex.Array,
    n: int,
    lambda_t: float = 1.0,
    stop_target_gradients: bool = True,
) -> chex.Array:
    """Computes strided n-step bootstrapped return targets over a batch of sequences.

    The returns are computed according to the below equation iterated `n` times:

        Gₜ = rₜ₊₁ + γₜ₊₁ [(1 - λₜ₊₁) vₜ₊₁ + λₜ₊₁ Gₜ₊₁].

    When lambda_t == 1. (default), this reduces to

        Gₜ = rₜ₊₁ + γₜ₊₁ * (rₜ₊₂ + γₜ₊₂ * (... * (rₜ₊ₙ + γₜ₊ₙ * vₜ₊ₙ ))).

    Args:
        r_t: rewards at times B x [1, ..., T].
        discount_t: discounts at times B x [1, ..., T].
        v_t: state or state-action values to bootstrap from at time B x [1, ...., T].
        n: number of steps over which to accumulate reward before bootstrapping.
        lambda_t: lambdas at times B x [1, ..., T]. Shape is [], or B x [T-1].
        stop_target_gradients: bool indicating whether or not to apply stop gradient
        to targets.

    Returns:
        estimated bootstrapped returns at times B x [0, ...., T-1]
    """
    # swap axes to make time axis the first dimension
    r_t, discount_t, v_t = jax.tree_map(lambda x: jnp.swapaxes(x, 0, 1), (r_t, discount_t, v_t))
    seq_len = r_t.shape[0]
    batch_size = r_t.shape[1]

    # Maybe change scalar lambda to an array.
    lambda_t = jnp.ones_like(discount_t) * lambda_t

    # Shift bootstrap values by n and pad end of sequence with last value v_t[-1].
    pad_size = min(n - 1, seq_len)
    targets = jnp.concatenate([v_t[n - 1 :], jnp.array([v_t[-1]] * pad_size)], axis=0)

    # Pad sequences. Shape is now (T + n - 1,).
    r_t = jnp.concatenate([r_t, jnp.zeros((n - 1, batch_size))], axis=0)
    discount_t = jnp.concatenate([discount_t, jnp.ones((n - 1, batch_size))], axis=0)
    lambda_t = jnp.concatenate([lambda_t, jnp.ones((n - 1, batch_size))], axis=0)
    v_t = jnp.concatenate([v_t, jnp.array([v_t[-1]] * (n - 1))], axis=0)

    # Work backwards to compute n-step returns.
    for i in reversed(range(n)):
        r_ = r_t[i : i + seq_len]
        discount_ = discount_t[i : i + seq_len]
        lambda_ = lambda_t[i : i + seq_len]
        v_ = v_t[i : i + seq_len]
        targets = r_ + discount_ * ((1.0 - lambda_) * v_ + lambda_ * targets)

    targets = jnp.swapaxes(targets, 0, 1)
    return jax.lax.select(stop_target_gradients, jax.lax.stop_gradient(targets), targets)


def batch_general_off_policy_returns_from_q_and_v(
    q_t: chex.Array,
    v_t: chex.Array,
    r_t: chex.Array,
    discount_t: chex.Array,
    c_t: chex.Array,
    stop_target_gradients: bool = False,
) -> chex.Array:
    """Calculates targets for various off-policy evaluation algorithms.

    Given a window of experience of length `K+1`, generated by a behaviour policy
    μ, for each time-step `t` we can estimate the return `G_t` from that step
    onwards, under some target policy π, using the rewards in the trajectory, the
    values under π of states and actions selected by μ, according to equation:

      Gₜ = rₜ₊₁ + γₜ₊₁ * (vₜ₊₁ - cₜ₊₁ * q(aₜ₊₁) + cₜ₊₁* Gₜ₊₁),

    where, depending on the choice of `c_t`, the algorithm implements:

      Importance Sampling             c_t = π(x_t, a_t) / μ(x_t, a_t),
      Harutyunyan's et al. Q(lambda)  c_t = λ,
      Precup's et al. Tree-Backup     c_t = π(x_t, a_t),
      Munos' et al. Retrace           c_t = λ min(1, π(x_t, a_t) / μ(x_t, a_t)).

    See "Safe and Efficient Off-Policy Reinforcement Learning" by Munos et al.
    (https://arxiv.org/abs/1606.02647).

    Args:
      q_t: Q-values under π of actions executed by μ at times [1, ..., K - 1].
      v_t: Values under π at times [1, ..., K].
      r_t: rewards at times [1, ..., K].
      discount_t: discounts at times [1, ..., K].
      c_t: weights at times [1, ..., K - 1].
      stop_target_gradients: bool indicating whether or not to apply stop gradient
        to targets.

    Returns:
      Off-policy estimates of the generalized returns from states visited at times
      [0, ..., K - 1].
    """
    q_t, v_t, r_t, discount_t, c_t = jax.tree_map(
        lambda x: jnp.swapaxes(x, 0, 1), (q_t, v_t, r_t, discount_t, c_t)
    )

    g = r_t[-1] + discount_t[-1] * v_t[-1]  # G_K-1.

    def _body(
        acc: chex.Array, xs: Tuple[chex.Array, chex.Array, chex.Array, chex.Array, chex.Array]
    ) -> Tuple[chex.Array, chex.Array]:
        reward, discount, c, v, q = xs
        acc = reward + discount * (v - c * q + c * acc)
        return acc, acc

    _, returns = jax.lax.scan(
        _body, g, (r_t[:-1], discount_t[:-1], c_t, v_t[:-1], q_t), reverse=True
    )
    returns = jnp.concatenate([returns, g[jnp.newaxis]], axis=0)

    returns = jnp.swapaxes(returns, 0, 1)
    return jax.lax.select(stop_target_gradients, jax.lax.stop_gradient(returns), returns)


def batch_retrace_continuous(
    q_tm1: chex.Array,
    q_t: chex.Array,
    v_t: chex.Array,
    r_t: chex.Array,
    discount_t: chex.Array,
    log_rhos: chex.Array,
    lambda_: Union[chex.Array, float],
    stop_target_gradients: bool = True,
) -> chex.Array:
    """Retrace continuous.

    See "Safe and Efficient Off-Policy Reinforcement Learning" by Munos et al.
    (https://arxiv.org/abs/1606.02647).

    Args:
      q_tm1: Q-values at times [0, ..., K - 1].
      q_t: Q-values evaluated at actions collected using behavior
        policy at times [1, ..., K - 1].
      v_t: Value estimates of the target policy at times [1, ..., K].
      r_t: reward at times [1, ..., K].
      discount_t: discount at times [1, ..., K].
      log_rhos: Log importance weight pi_target/pi_behavior evaluated at actions
        collected using behavior policy [1, ..., K - 1].
      lambda_: scalar or a vector of mixing parameter lambda.
      stop_target_gradients: bool indicating whether or not to apply stop gradient
        to targets.

    Returns:
      Retrace error.
    """

    c_t = jnp.minimum(1.0, jnp.exp(log_rhos)) * lambda_

    # The generalized returns are independent of Q-values and cs at the final
    # state.
    target_tm1 = batch_general_off_policy_returns_from_q_and_v(q_t, v_t, r_t, discount_t, c_t)

    target_tm1 = jax.lax.select(
        stop_target_gradients, jax.lax.stop_gradient(target_tm1), target_tm1
    )
    return target_tm1 - q_tm1
