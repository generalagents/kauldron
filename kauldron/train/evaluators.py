# Copyright 2023 The kauldron Authors.
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

"""Evaluators."""

from __future__ import annotations

import collections.abc
import dataclasses
import functools
from typing import Mapping, Optional, TypeVar

import flax
import jax
from jax import numpy as jnp
from kauldron import data
from kauldron import kontext
from kauldron import losses as losses_lib
from kauldron import metrics as metrics_lib
from kauldron import summaries as summaries_lib
from kauldron.metrics import base
from kauldron.metrics import base_state
from kauldron.train import config_lib
from kauldron.train import metric_writer
from kauldron.train import rngs_lib
from kauldron.train import train_lib
from kauldron.train import train_step
from kauldron.typing import Array, typechecked  # pylint: disable=g-multiple-import,g-importing-member
from kauldron.utils import config_util
from kauldron.utils import jax_utils
from kauldron.utils import utils
from kauldron.utils.sharding_utils import sharding  # pylint: disable=g-importing-member
import numpy as np


_SelfT = TypeVar('_SelfT')

_DEFAULT_EVAL_NAME = 'eval'
_DEFAULT_FEWSHOT_EVAL_NAME = 'fewshot_eval'


class Evaluator(config_util.BaseConfig, config_util.UpdateFromRootCfg):
  """Evaluator running `num_batches` times every `run_every` steps.

  If not provided, losses, metrics, summaries are reused from train.

  Usage:

  ```
  evaluator = kd.train.Evaluator(
      run_every=100,
      ds=test_ds,
      base_cfg=cfg,
  )
  evaluator.maybe_eval(step=0, state=state)
  ```

  Attributes:
    name: Eval name (display in TensorBoard)
    run_every: Run eval every `run_every` train steps
    num_batches: How many batches to run evaluation on. Use `None` to evaluate
      on the full test dataset. Note that each evaluation reinitializes the
      dataset iterator, so setting to `1` will run all evaluations on the same
      batch.
    ds: Dataset to evaluate on.
    losses: Losses
    metrics: Metrics
    summaries: Summaries
  """

  name: str = _DEFAULT_EVAL_NAME
  run_every: int
  num_batches: Optional[int]
  ds: data.Pipeline = config_util.ROOT_CFG_REF.eval_ds
  losses: dict[str, losses_lib.Loss] = config_util.ROOT_CFG_REF.train_losses
  metrics: dict[str, metrics_lib.Metric] = (
      config_util.ROOT_CFG_REF.train_metrics
  )
  summaries: dict[str, summaries_lib.Summary] = (
      config_util.ROOT_CFG_REF.train_summaries
  )

  base_cfg: config_lib.Config = dataclasses.field(
      default=config_util.ROOT_CFG_REF, repr=False
  )

  # TODO(klausg): filter out metrics / summaries that access grads/updates

  def update_from_root_cfg(self: _SelfT, root_cfg: config_lib.Config) -> _SelfT:
    """See base class."""
    new_self = super().update_from_root_cfg(root_cfg)
    if new_self.ds is None:
      raise ValueError(
          f'Eval dataset missing (`cfg.evals.{self.name}.ds is None`). Please'
          ' set it either in `kd.train.Config.eval_ds` or in'
          ' `Evaluator(ds=...)`.'
      )
    return new_self.replace(
        ds=new_self.ds.update_from_root_cfg(root_cfg),
    )

  def maybe_eval(
      self, *, step: int, state: train_step.TrainState
  ) -> train_step.Auxiliaries | None:
    """See base class."""
    if self.should_eval(step):
      return self.evaluate(state, step)

  def should_eval(self, step: int) -> bool:
    return step % self.run_every == 0

  def evaluate(
      self, state: train_step.TrainState, step: int
  ) -> train_step.Auxiliaries:
    """Run one full evaluation."""
    self._assert_root_cfg_resolved()

    merged_aux = None
    for eval_step, batch in utils.enum_iter(
        self.ds,
        total_steps=self.num_batches,
        desc='eval',
    ):
      eval_step = sharding.device_put(eval_step, sharding.REPLICATED)
      aux = _step(
          model_with_aux=self.model_with_aux,
          rng_streams=self.base_cfg.rng_streams,
          eval_step=eval_step,
          state=state,
          batch=batch,
      )
      # Merge/accumulate all states
      # By default, cross-process communication is only allowed inside
      # `jax.jit` but clu metric do not support `jax.jit`:
      # https://github.com/google/CommonLoopUtils/tree/HEAD/clu/metrics.py;l=383;rcl=559340497
      # So we locally allow cross-process communication for merging the
      # metrics
      with jax.spmd_mode('allow_all'), jax.transfer_guard('allow'):
        merged_aux = merged_aux | aux
    assert merged_aux is not None  # At least one iteration

    train_lib.write_summaries(
        writer=self.writer,
        step=step,
        aux=merged_aux,
        schedules={},
        model_with_aux=self.model_with_aux,
        log_summaries=True,
    )
    return merged_aux

  @functools.cached_property
  def model_with_aux(self) -> train_step.ModelWithAux:
    """Model which also compute the auxiliaries (losses, metrics,...)."""
    return train_step.ModelWithAux(
        model=self.base_cfg.model,
        losses=self.losses,
        metrics=self.metrics,
        summaries=self.summaries,
    )

  @functools.cached_property
  def writer(self) -> metric_writer.KDMetricWriter:
    """Metric writer."""
    return metric_writer.KDMetricWriter(
        workdir=self.base_cfg.workdir, collection=self.name
    )


@jax_utils.jit(
    static_argnames=('model_with_aux', 'rng_streams'),
    # in_shardings=lambda: dict(  # pylint: disable=g-long-lambda
    #     eval_step=sharding.REPLICATED,
    #     state=sharding.REPLICATED,
    #     batch=sharding.SHARDED,
    # ),
    out_shardings=lambda: sharding.REPLICATED,
)
def _step(
    *,
    model_with_aux: train_step.ModelWithAux,
    rng_streams: rngs_lib.RngStreams,
    eval_step: int,
    state: train_step.TrainState,
    batch,
) -> train_step.Auxiliaries:
  """Call the model (pmap version)."""
  _, ctx = model_with_aux.forward(
      params=state.params,
      batch=batch,
      rngs=rng_streams.eval_rngs(eval_step),
      step=state.step,  # Step is train step, NOT eval
      is_training=False,
  )
  aux = model_with_aux.get_aux(
      ctx,
      return_losses=True,
      return_metrics=True,
      return_summaries=True,
  )
  return aux


def normalize_evaluators(
    evaluators: collections.abc.Mapping[str, Evaluator]
) -> collections.abc.Mapping[str, Evaluator]:
  """Set the evaluator names."""
  if not isinstance(evaluators, collections.abc.Mapping):
    raise TypeError(
        '`cfg.evals` should be a `dict[str, Evaluator]`. Got:'
        f' {type(evaluators)}'
    )
  return {k: _replace_name(c, k) for k, c in evaluators.items()}


def _replace_name(evaluator: Evaluator, name: str) -> Evaluator:
  """Set the `evaluator.name`."""
  # TODO(klausg): factor out baseclass
  if not (
      isinstance(evaluator, Evaluator)
      or isinstance(evaluator, FewShotEvaluator)
  ):
    raise TypeError(
        'Eval values should be `kd.train.Evaluator`. Got:'
        f' {name}={type(evaluator)}'
    )
  elif name == 'train':
    raise ValueError(
        'Evaluator cannot be named `train` as it conflict with training'
        ' metrics.'
    )
  # TODO(epot): generalize or remove default name mechanism
  elif evaluator.name in [
      _DEFAULT_EVAL_NAME,
      _DEFAULT_FEWSHOT_EVAL_NAME,
  ]:  # Default name, overwrite
    return dataclasses.replace(evaluator, name=name)
  elif evaluator.name == name:
    return evaluator
  else:
    raise ValueError(
        f'Evaluator name provided should match. Got: {evaluator.name} != {name}'
    )


# TODO(adosovitskiy) move to separate file once evaluator base class is in place
class FewShotEvaluator(config_util.BaseConfig, config_util.UpdateFromRootCfg):
  """FewShotEvaluator running closed-form few-shot classification.

  Compute the features from the model, solve closed-form L2-regularized linear
  regression for few-shot classification. This is fairly fast, so can be run
  regularly during training.

  Following (and largely copying) https://github.com/google-research/big_vision

  Attributes:
    name: Eval name (display in TensorBoard)
    run_every: Run eval every `run_every` train steps
    ds_train: Dataset to train few-shot classification on
    ds_train: Dataset to validate few-shot classification on (to select L2 reg)
    ds_train: Dataset to test few-shot classification on
    metric_prefix: String prefix to be used for the metrics from this evaluator
    num_classes: Number of classes in the classification task
    num_shots: A sequence of integers - numbers of shots to be evaluated
    repr_names: A dictionary of representations to be evaluated. Keys are names
      to be used to refer to the representations, values are paths in the
      context from which to take the actual features
    label_name: key by which to get the labels from the context
    selected_repr: a key from repr_names for which to put the accuracies to the
      main metrics
    seed: random seed for selecting the training data subset


  Usage example:
    "fewshot_i1k": kd.train.evaluators.FewShotEvaluator(
        run_every=10_000,
        metric_prefix="i1k",
        ds_train=_make_i1k_fewshot(split="train[:-10000]", batch_size=4096),
        ds_val=_make_i1k_fewshot(split="train[-10000:]", batch_size=4096),
        ds_test=_make_i1k_fewshot(split="validation", batch_size=4096),
        num_classes=1000,
        num_shots=(1, 2, 5, 10),
        repr_names={"pre_logits": "interms.pre_logits.__call__[0]"},
        label_name="batch.label",
    )
  """

  name: str = _DEFAULT_FEWSHOT_EVAL_NAME
  run_every: int
  ds_train: data.TFDataPipeline
  ds_val: data.TFDataPipeline
  ds_test: data.TFDataPipeline
  metric_prefix: str
  num_classes: int
  num_shots: tuple[int]
  repr_names: Mapping[str, str] = dataclasses.field(
      default_factory=flax.core.FrozenDict
  )
  label_name: str
  selected_repr: str = 'pre_logits'
  seed: int = 17

  base_cfg: config_lib.Config = dataclasses.field(
      default=config_util.ROOT_CFG_REF, repr=False
  )

  def maybe_eval(
      self, *, step: int, state: train_step.TrainState
  ) -> train_step.Auxiliaries | None:
    """See base class."""
    if self.should_eval(step):
      return self.evaluate(state, step)

  def should_eval(self, step: int) -> bool:
    return step % self.run_every == 0

  @property
  def metrics(self):
    # This is a hack to make the metrics show up on Flatboard
    return {
        f'{self.metric_prefix}-{shots}shot': 'blah' for shots in self.num_shots
    }

  def evaluate(self, state: train_step.TrainState, step: int):
    """Run one full evaluation."""
    self._assert_root_cfg_resolved()

    train_features, train_labels = self.compute_features(state, self.ds_train)
    val_features, val_labels = self.compute_features(state, self.ds_val)
    test_features, test_labels = self.compute_features(state, self.ds_test)

    fewshot_accuracies = {}
    l2_regs = 2 ** np.arange(-10, 10, dtype=np.float32)
    for feat_key in train_features.keys():
      print(feat_key)
      curr_results_val, curr_results_test = run_fewshot(
          train_features[feat_key],
          train_labels,
          val_features[feat_key],
          val_labels,
          test_features[feat_key],
          test_labels,
          num_classes=self.num_classes,
          all_shots=self.num_shots,
          l2_regs=l2_regs,
          seed=self.seed,
      )
      print(curr_results_val, curr_results_test)
      for shots in self.num_shots:
        best_reg = np.argmax(curr_results_val[shots])
        if feat_key == self.selected_repr:
          fewshot_accuracies[f'metrics/{self.metric_prefix}-{shots}shot'] = (
              curr_results_test[shots][best_reg]
          )
        fewshot_accuracies[
            f'z_fewshot_all/{self.metric_prefix}-{feat_key}-{shots}shot'
        ] = curr_results_test[shots][best_reg]
        for acc, l2_reg in zip(curr_results_test[shots], l2_regs):
          fewshot_accuracies[
              f'z_fewshot_all/z_{self.metric_prefix}-{feat_key}-{shots}shot-{l2_reg:.5}'
          ] = acc

    with jax.transfer_guard('allow'):
      self.writer.write_scalars(
          step=step,
          scalars=fewshot_accuracies,
      )
    return None

  def compute_features(self, state, ds):
    merged_aux = None
    for eval_step, batch in utils.enum_iter(ds):
      eval_step = sharding.device_put(eval_step, sharding.REPLICATED)
      aux = _step(
          model_with_aux=self.model_with_aux,
          rng_streams=self.base_cfg.rng_streams,
          eval_step=eval_step,
          state=state,
          batch=batch,
      )
      # Merge/accumulate all states
      if merged_aux is None:
        merged_aux = aux
      else:
        # By default, cross-process communication is only allowed inside
        # `jax.jit` but clu metric do not support `jax.jit`:
        # https://github.com/google/CommonLoopUtils/tree/HEAD/clu/metrics.py;l=383;rcl=559340497
        # So we locally allow cross-process communication for merging the
        # metrics
        with jax.spmd_mode('allow_all'), jax.transfer_guard('allow'):
          merged_aux = merged_aux.merge(aux)
    assert merged_aux is not None  # At least one iteration
    merged_summaries = merged_aux.compute()
    features = {
        k.removeprefix('metrics/'): v
        for k, v in merged_summaries.metric_values.items()
    }
    labels = features.pop('labels')
    return features, labels

  @functools.cached_property
  def model_with_aux(self) -> train_step.ModelWithAux:
    """Model which also compute the auxiliaries (losses, metrics,...)."""
    return train_step.ModelWithAux(
        model=self.base_cfg.model,
        metrics=flax.core.FrozenDict(
            {
                key: ComputeFeaturesMetric(features=feature)
                for key, feature in self.repr_names.items()
            }
            | {'labels': ComputeFeaturesMetric(features=self.label_name)}
        ),
        losses=flax.core.FrozenDict({}),
        summaries=flax.core.FrozenDict({}),
    )

  @functools.cached_property
  def writer(self) -> metric_writer.KDMetricWriter:
    """Metric writer."""
    return metric_writer.KDMetricWriter(
        workdir=self.base_cfg.workdir, collection=self.name
    )


@dataclasses.dataclass(kw_only=True, frozen=True, eq=True)
class ComputeFeaturesMetric(base.Metric):
  """Compute the features over a dataset."""

  features: kontext.Key

  @flax.struct.dataclass
  class State(base_state.CollectingState):
    features: Array['...']

    @typechecked
    def compute(self):
      return np.array(super().compute().features)

  @typechecked
  def get_state(
      self,
      features: Array['...'],
  ) -> ComputeFeaturesMetric.State:
    # simply collect the given values
    return self.State(features=features)


BIAS_CONSTANT = 100.0


def to_cpu(x):
  return jax.device_put(x, jax.local_devices(backend='cpu')[0])


def run_fewshot(
    x_train_all,
    y_train_all,
    x_val,
    y_val,
    x_test,
    y_test,
    num_classes=None,
    all_shots=tuple(),
    l2_regs=tuple(),
    seed=17,
):
  """Run few-shot evaluation."""
  rng = np.random.default_rng(seed)

  class_indices = [
      rng.permutation(np.where(y_train_all == cls_i)[0])
      for cls_i in range(num_classes)
  ]

  results_val = {}
  results_test = {}
  for shots in all_shots:
    all_idx = [indices[:shots] for indices in class_indices]
    all_idx = np.concatenate(all_idx, axis=0)
    assert len(all_idx) == num_classes * shots, (
        f'expected {num_classes * shots} training samples for'
        f' {num_classes} classes and {shots} shots, instead got {len(all_idx)}'
    )
    x = x_train_all[all_idx]
    y = y_train_all[all_idx]

    print(f'[fewshot][i1k][{shots}-shot]: compute cache')
    cache = _precompute_cache(to_cpu(x), to_cpu(y), num_classes)
    curr_results_val = []
    curr_results_test = []
    for l2_reg in l2_regs:
      acc_val = _eig_fewshot_acc_fn(
          cache, to_cpu(x_val), to_cpu(y_val), to_cpu(l2_reg)
      )
      curr_results_val.append(acc_val)
      acc_test = _eig_fewshot_acc_fn(
          cache, to_cpu(x_test), to_cpu(y_test), to_cpu(l2_reg)
      )
      curr_results_test.append(acc_test)
    results_val[shots] = np.stack(curr_results_val)
    results_test[shots] = np.stack(curr_results_test)
  return results_val, results_test


# The below functions are copied from
# https://github.com/google-research/big_vision/blob/main/big_vision/evaluators/fewshot_lsr.py


# Setup function for few-shot regression on CPU to avoid "polluting" the TPU.
@functools.partial(jax.jit, backend='cpu', static_argnums=(2,))
def _precompute_cache(x, y, num_classes):
  """Cache quantities to speed-up the computation of L2-regularized least-sq."""
  # Whiten
  mean = jnp.mean(x, axis=0, keepdims=True)
  std = jnp.std(x, axis=0, keepdims=True) + 1e-5
  x = (x - mean) / std

  # Add a constant feature for the bias, large so it's almost unregularized:
  x = jnp.pad(x, ((0, 0), (0, 1)), constant_values=BIAS_CONSTANT)

  # To one-hot representation rescaled into {-1, 1}
  y = 2.0 * jax.nn.one_hot(y, num_classes) - 1.0

  num_points, dim = x.shape
  # Let N be the number of points, D the dimension and C the number of classes.
  # We have x of shape (N, D) and y of shape (N, C).
  # For least-squares, we can compute
  #
  #   (A) when N >= D, (x^T x + l2 Id)^{-1} x^T y
  #   (B) when D > N, x^T  (x x^T + l2 Id)^{-1} y
  #
  # We pre-compute the eigen-decomposition of either x^T x or x x^T which
  # becomes q diag(eigs) q^T with q unitary matrix either (D, D) or (N, N)
  # and eigs a vector (D,) or (N,).
  #
  # For any l2 > 0, we can compute (x^T x + l2 Id)^{-1} or (x x^T + l2 Id)^{-1}
  # by simply computing q (diag(eigs) + l2 Id)^{-1} q^T.
  # (SVD would be more natural here, but it proved slower, so we use eigh)
  #
  # Both cases (A) and (B) can be viewed as lhs (diag(eigs) + l2 Id)^{-1} rhs,
  # where lhs/rhs are pre-computed left/right-hand sides to specify.
  #
  if num_points >= dim:
    eigs, q = jnp.linalg.eigh(x.T @ x)
    rhs = q.T @ (x.T @ y)
    lhs = q
  else:
    eigs, q = jnp.linalg.eigh(x @ x.T)
    rhs = q.T @ y
    lhs = x.T @ q

  cache = {'eigs': eigs, 'rhs': rhs, 'lhs': lhs, 'mean': mean, 'std': std}
  return cache


@functools.partial(jax.jit, backend='cpu')
def _eig_fewshot_acc_fn(cache, x_test, y_test, l2_reg):
  """Computes (x,y) linear regression accuracy on (x_test, y_test)."""

  x_test = (x_test - cache['mean']) / cache['std']
  x_test = jnp.pad(x_test, ((0, 0), (0, 1)), constant_values=BIAS_CONSTANT)

  rhs = cache['rhs']
  lhs = cache['lhs']
  eigs = cache['eigs']

  # See comments in _precompute_cache for context about the formula.
  scaling = 1.0 / (eigs + l2_reg * jnp.ones_like(eigs))
  scaling = scaling.reshape((1, -1))
  w = (lhs * scaling) @ rhs
  # Predict test-set values and measure their accuracy
  preds = jnp.argmax(x_test @ w, axis=1)
  return jnp.mean(preds == y_test)
