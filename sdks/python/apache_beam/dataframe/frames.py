#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import collections
import math

import pandas as pd

from apache_beam.dataframe import expressions
from apache_beam.dataframe import frame_base
from apache_beam.dataframe import io
from apache_beam.dataframe import partitionings


class DeferredDataFrameOrSeries(frame_base.DeferredFrame):
  def __array__(self, dtype=None):
    raise frame_base.WontImplementError(
        'Conversion to a non-deferred a numpy array.')


@frame_base.DeferredFrame._register_for(pd.Series)
class DeferredSeries(DeferredDataFrameOrSeries):
  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def align(self, other, join, axis, level, method, **kwargs):
    if level is not None:
      raise NotImplementedError('per-level align')
    if method is not None:
      raise frame_base.WontImplementError('order-sensitive')
    # We're using pd.concat here as expressions don't yet support
    # multiple return values.
    aligned = frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'align',
            lambda x,
            y: pd.concat([x, y], axis=1, join='inner'),
            [self._expr, other._expr],
            requires_partition_by=partitionings.Index(),
            preserves_partition_by=partitionings.Index()))
    return aligned.iloc[:, 0], aligned.iloc[:, 1]

  astype = frame_base._elementwise_method('astype')

  between = frame_base._elementwise_method('between')

  def dot(self, other):
    left = self._expr
    if isinstance(other, DeferredSeries):
      right = expressions.ComputedExpression(
          'to_dataframe',
          pd.DataFrame, [other._expr],
          requires_partition_by=partitionings.Nothing(),
          preserves_partition_by=partitionings.Index())
      right_is_series = True
    elif isinstance(other, DeferredDataFrame):
      right = other._expr
      right_is_series = False
    else:
      raise frame_base.WontImplementError('non-deferred result')

    dots = expressions.ComputedExpression(
        'dot',
        # Transpose so we can sum across rows.
        (lambda left, right: pd.DataFrame(left @ right).T),
        [left, right],
        requires_partition_by=partitionings.Index())
    with expressions.allow_non_parallel_operations(True):
      sums = expressions.ComputedExpression(
          'sum',
          lambda dots: dots.sum(),  #
          [dots],
          requires_partition_by=partitionings.Singleton())

      if right_is_series:
        result = expressions.ComputedExpression(
            'extract',
            lambda df: df[0], [sums],
            requires_partition_by=partitionings.Singleton())
      else:
        result = sums
      return frame_base.DeferredFrame.wrap(result)

  __matmul__ = dot

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def std(self, axis, skipna, level, ddof, **kwargs):
    if level is not None:
      raise NotImplementedError("per-level aggregation")
    if skipna is None or skipna:
      self = self.dropna()  # pylint: disable=self-cls-assignment

    # See the online, numerically stable formulae at
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm
    # and
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_online_algorithm
    def compute_moments(x):
      n = len(x)
      m = x.std(ddof=0)**2 * n
      s = x.sum()
      return pd.DataFrame(dict(m=[m], s=[s], n=[n]))

    def combine_moments(data):
      m = s = n = 0.0
      for datum in data.itertuples():
        if datum.n == 0:
          continue
        elif n == 0:
          m, s, n = datum.m, datum.s, datum.n
        else:
          delta = s / n - datum.s / datum.n
          m += datum.m + delta**2 * n * datum.n / (n + datum.n)
          s += datum.s
          n += datum.n
      if n <= ddof:
        return float('nan')
      else:
        return math.sqrt(m / (n - ddof))

    moments = expressions.ComputedExpression(
        'compute_moments',
        compute_moments, [self._expr],
        requires_partition_by=partitionings.Nothing())
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'combine_moments',
              combine_moments, [moments],
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def corr(self, other, method, min_periods):
    if method == 'pearson':  # Note that this is the default.
      x, y = self.dropna().align(other.dropna(), 'inner')
      return x._corr_aligned(y, min_periods)

    else:
      # The rank-based correlations are not obviously parallelizable, though
      # perhaps an approximation could be done with a knowledge of quantiles
      # and custom partitioning.
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'corr',
              lambda df,
              other: df.corr(other, method=method, min_periods=min_periods),
              [self._expr, other._expr],
              requires_partition_by=partitionings.Singleton()))

  def _corr_aligned(self, other, min_periods):
    std_x = self.std()
    std_y = other.std()
    cov = self._cov_aligned(other, min_periods)
    return cov.apply(
        lambda cov, std_x, std_y: cov / (std_x * std_y), args=[std_x, std_y])

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def cov(self, other, min_periods, ddof):
    x, y = self.dropna().align(other.dropna(), 'inner')
    return x._cov_aligned(y, min_periods, ddof)

  def _cov_aligned(self, other, min_periods, ddof=1):
    # Use the formulae from
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Covariance
    def compute_co_moments(x, y):
      n = len(x)
      if n <= 1:
        c = 0
      else:
        c = x.cov(y) * (n - 1)
      sx = x.sum()
      sy = y.sum()
      return pd.DataFrame(dict(c=[c], sx=[sx], sy=[sy], n=[n]))

    def combine_co_moments(data):
      c = sx = sy = n = 0.0
      for datum in data.itertuples():
        if datum.n == 0:
          continue
        elif n == 0:
          c, sx, sy, n = datum.c, datum.sx, datum.sy, datum.n
        else:
          c += (
              datum.c + (sx / n - datum.sx / datum.n) *
              (sy / n - datum.sy / datum.n) * n * datum.n / (n + datum.n))
          sx += datum.sx
          sy += datum.sy
          n += datum.n
      if n < max(2, ddof, min_periods or 0):
        return float('nan')
      else:
        return c / (n - ddof)

    moments = expressions.ComputedExpression(
        'compute_co_moments',
        compute_co_moments, [self._expr, other._expr],
        requires_partition_by=partitionings.Index())

    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'combine_co_moments',
              combine_co_moments, [moments],
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  @frame_base.maybe_inplace
  def dropna(self, **kwargs):
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'dropna',
            lambda df: df.dropna(**kwargs), [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Nothing()))

  items = iteritems = frame_base.wont_implement_method('non-lazy')

  isin = frame_base._elementwise_method('isin')

  isna = frame_base._elementwise_method('isna')
  notnull = notna = frame_base._elementwise_method('notna')

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  @frame_base.maybe_inplace
  def fillna(self, value, method):
    if method is not None:
      raise frame_base.WontImplementError('order-sensitive')
    if isinstance(value, frame_base.DeferredBase):
      value_expr = value._expr
    else:
      value_expr = expressions.ConstantExpression(value)
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'fillna',
            lambda df,
            value: df.fillna(value, method=method), [self._expr, value_expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Nothing()))

  reindex = frame_base.not_implemented_method('reindex')

  to_numpy = to_string = frame_base.wont_implement_method('non-deferred value')

  transform = frame_base._elementwise_method(
      'transform', restrictions={'axis': 0})

  def aggregate(self, func, axis=0, *args, **kwargs):
    if isinstance(func, list) and len(func) > 1:
      # Aggregate each column separately, then stick them all together.
      rows = [self.agg([f], *args, **kwargs) for f in func]
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'join_aggregate',
              lambda *rows: pd.concat(rows), [row._expr for row in rows]))
    else:
      # We're only handling a single column.
      base_func = func[0] if isinstance(func, list) else func
      if _is_associative(base_func) and not args and not kwargs:
        intermediate = expressions.elementwise_expression(
            'pre_aggregate',
            lambda s: s.agg([base_func], *args, **kwargs), [self._expr])
        allow_nonparallel_final = True
      else:
        intermediate = self._expr
        allow_nonparallel_final = None  # i.e. don't change the value
      with expressions.allow_non_parallel_operations(allow_nonparallel_final):
        return frame_base.DeferredFrame.wrap(
            expressions.ComputedExpression(
                'aggregate',
                lambda s: s.agg(func, *args, **kwargs), [intermediate],
                preserves_partition_by=partitionings.Singleton(),
                requires_partition_by=partitionings.Singleton()))

  agg = aggregate

  all = frame_base._agg_method('all')
  any = frame_base._agg_method('any')
  min = frame_base._agg_method('min')
  max = frame_base._agg_method('max')
  prod = product = frame_base._agg_method('prod')
  sum = frame_base._agg_method('sum')

  cummax = cummin = cumsum = cumprod = frame_base.wont_implement_method(
      'order-sensitive')
  diff = frame_base.wont_implement_method('order-sensitive')

  head = tail = frame_base.wont_implement_method('order-sensitive')

  memory_usage = frame_base.wont_implement_method('non-deferred value')

  # In Series __contains__ checks the index
  __contains__ = frame_base.wont_implement_method('non-deferred value')

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def nlargest(self, keep, **kwargs):
    # TODO(robertwb): Document 'any' option.
    # TODO(robertwb): Consider (conditionally) defaulting to 'any' if no
    # explicit keep parameter is requested.
    if keep == 'any':
      keep = 'first'
    elif keep != 'all':
      raise frame_base.WontImplementError('order-sensitive')
    kwargs['keep'] = keep
    per_partition = expressions.ComputedExpression(
        'nlargest-per-partition',
        lambda df: df.nlargest(**kwargs), [self._expr],
        preserves_partition_by=partitionings.Singleton(),
        requires_partition_by=partitionings.Nothing())
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'nlargest',
              lambda df: df.nlargest(**kwargs), [per_partition],
              preserves_partition_by=partitionings.Singleton(),
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  def nsmallest(self, keep, **kwargs):
    if keep == 'any':
      keep = 'first'
    elif keep != 'all':
      raise frame_base.WontImplementError('order-sensitive')
    kwargs['keep'] = keep
    per_partition = expressions.ComputedExpression(
        'nsmallest-per-partition',
        lambda df: df.nsmallest(**kwargs), [self._expr],
        preserves_partition_by=partitionings.Singleton(),
        requires_partition_by=partitionings.Nothing())
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'nsmallest',
              lambda df: df.nsmallest(**kwargs), [per_partition],
              preserves_partition_by=partitionings.Singleton(),
              requires_partition_by=partitionings.Singleton()))

  rename_axis = frame_base._elementwise_method('rename_axis')

  @frame_base.args_to_kwargs(pd.Series)
  @frame_base.populate_defaults(pd.Series)
  @frame_base.maybe_inplace
  def replace(self, limit, **kwargs):
    if limit is None:
      requires_partition_by = partitionings.Nothing()
    else:
      requires_partition_by = partitionings.Singleton()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'replace',
            lambda df: df.replace(limit=limit, **kwargs), [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  round = frame_base._elementwise_method('round')

  searchsorted = frame_base.wont_implement_method('order-sensitive')

  shift = frame_base.wont_implement_method('order-sensitive')

  take = frame_base.wont_implement_method('deprecated')

  to_dict = frame_base.wont_implement_method('non-deferred')

  to_frame = frame_base._elementwise_method('to_frame')

  def unique(self, as_series=False):
    if not as_series:
      raise frame_base.WontImplementError(
          'pass as_series=True to get the result as a (deferred) Series')
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'unique',
            lambda df: pd.Series(df.unique()), [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Singleton()))

  def update(self, other):
    self._expr = expressions.ComputedExpression(
        'update',
        lambda df,
        other: df.update(other) or df, [self._expr, other._expr],
        preserves_partition_by=partitionings.Singleton(),
        requires_partition_by=partitionings.Index())

  unstack = frame_base.wont_implement_method('non-deferred column values')

  values = property(frame_base.wont_implement_method('non-deferred'))

  view = frame_base.wont_implement_method('memory sharing semantics')

  @property
  def str(self):
    return _DeferredStringMethods(self._expr)


for name in ['apply', 'map', 'transform']:
  setattr(DeferredSeries, name, frame_base._elementwise_method(name))


@frame_base.DeferredFrame._register_for(pd.DataFrame)
class DeferredDataFrame(DeferredDataFrameOrSeries):
  @property
  def T(self):
    return self.transpose()

  @property
  def columns(self):
    return self._expr.proxy().columns

  def groupby(self, by):
    # TODO: what happens to the existing index?
    # We set the columns to index as we have a notion of being partitioned by
    # index, but not partitioned by an arbitrary subset of columns.
    return DeferredGroupBy(
        expressions.ComputedExpression(
            'groupbyindex',
            lambda df: df.groupby(level=list(range(df.index.nlevels))),
            [self.set_index(by)._expr],
            requires_partition_by=partitionings.Index(),
            preserves_partition_by=partitionings.Singleton()))

  def __getattr__(self, name):
    # Column attribute access.
    if name in self._expr.proxy().columns:
      return self[name]
    else:
      return object.__getattribute__(self, name)

  def __getitem__(self, key):
    # TODO: Replicate pd.DataFrame.__getitem__ logic
    if isinstance(key, frame_base.DeferredBase):
      # Fail early if key is a DeferredBase as it interacts surprisingly with
      # key in self._expr.proxy().columns
      raise NotImplementedError(
          "Indexing with a deferred frame is not yet supported. Consider "
          "using df.loc[...]")

    if (isinstance(key, list) and
        all(key_column in self._expr.proxy().columns
            for key_column in key)) or key in self._expr.proxy().columns:
      return self._elementwise(lambda df: df[key], 'get_column')
    else:
      raise NotImplementedError(key)

  def __contains__(self, key):
    # Checks if proxy has the given column
    return self._expr.proxy().__contains__(key)

  def __setitem__(self, key, value):
    if isinstance(key, str):
      # yapf: disable
      return self._elementwise(
          lambda df, key, value: df.__setitem__(key, value),
          'set_column',
          (key, value),
          inplace=True)
    else:
      raise NotImplementedError(key)

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def set_index(self, keys, **kwargs):
    if isinstance(keys, str):
      keys = [keys]
    if not set(keys).issubset(self._expr.proxy().columns):
      raise NotImplementedError(keys)
    return frame_base.DeferredFrame.wrap(
      expressions.ComputedExpression(
          'set_index',
          lambda df: df.set_index(keys, **kwargs),
          [self._expr],
          requires_partition_by=partitionings.Nothing(),
          preserves_partition_by=partitionings.Nothing()))

  at = frame_base.not_implemented_method('at')

  @property
  def loc(self):
    return _DeferredLoc(self)

  @property
  def iloc(self):
    return _DeferredILoc(self)

  _get_index = _set_index = frame_base.not_implemented_method('index')
  index = property(_get_index, _set_index)

  @property
  def axes(self):
    return (self.index, self.columns)

  apply = frame_base.not_implemented_method('apply')
  explode = frame_base.not_implemented_method('explode')
  isin = frame_base.not_implemented_method('isin')
  assign = frame_base.not_implemented_method('assign')
  append = frame_base.not_implemented_method('append')
  combine = frame_base.not_implemented_method('combine')
  combine_first = frame_base.not_implemented_method('combine_first')
  count = frame_base.not_implemented_method('count')
  drop = frame_base.not_implemented_method('drop')
  eval = frame_base.not_implemented_method('eval')
  reindex = frame_base.not_implemented_method('reindex')
  melt = frame_base.not_implemented_method('melt')
  pivot = frame_base.not_implemented_method('pivot')
  pivot_table = frame_base.not_implemented_method('pivot_table')

  def aggregate(self, func, axis=0, *args, **kwargs):
    if axis is None:
      # Aggregate across all elements by first aggregating across columns,
      # then across rows.
      return self.agg(func, *args, **dict(kwargs, axis=1)).agg(
          func, *args, **dict(kwargs, axis=0))
    elif axis in (1, 'columns'):
      # This is an easy elementwise aggregation.
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'aggregate',
              lambda df: df.agg(func, axis=1, *args, **kwargs),
              [self._expr],
              requires_partition_by=partitionings.Nothing()))
    elif len(self._expr.proxy().columns) == 0 or args or kwargs:
      # For these corner cases, just colocate everything.
      return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'aggregate',
            lambda df: df.agg(func, *args, **kwargs),
            [self._expr],
            requires_partition_by=partitionings.Singleton()))
    else:
      # In the general case, compute the aggregation of each column separately,
      # then recombine.
      if not isinstance(func, dict):
        col_names = list(self._expr.proxy().columns)
        func = {col: func for col in col_names}
      else:
        col_names = list(func.keys())
      aggregated_cols = []
      for col in col_names:
        funcs = func[col]
        if not isinstance(funcs, list):
          funcs = [funcs]
        aggregated_cols.append(self[col].agg(funcs, *args, **kwargs))
      # The final shape is different depending on whether any of the columns
      # were aggregated by a list of aggregators.
      with expressions.allow_non_parallel_operations():
        if any(isinstance(funcs, list) for funcs in func.values()):
          return frame_base.DeferredFrame.wrap(
              expressions.ComputedExpression(
                  'join_aggregate',
                  lambda *cols: pd.DataFrame(
                      {col: value for col, value in zip(col_names, cols)}),
                  [col._expr for col in aggregated_cols],
                  requires_partition_by=partitionings.Singleton()))
        else:
          return frame_base.DeferredFrame.wrap(
            expressions.ComputedExpression(
                'join_aggregate',
                  lambda *cols: pd.Series(
                      {col: value[0] for col, value in zip(col_names, cols)}),
                [col._expr for col in aggregated_cols],
                requires_partition_by=partitionings.Singleton(),
                proxy=self._expr.proxy().agg(func, *args, **kwargs)))

  agg = aggregate

  applymap = frame_base._elementwise_method('applymap')

  memory_usage = frame_base.wont_implement_method('non-deferred value')
  info = frame_base.wont_implement_method('non-deferred value')

  all = frame_base._agg_method('all')
  any = frame_base._agg_method('any')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def corr(self, method, min_periods):
    if method == 'pearson':
      proxy = self._expr.proxy().corr()
      columns = list(proxy.columns)
      args = []
      arg_indices = []
      for ix, col1 in enumerate(columns):
        for col2 in columns[ix+1:]:
          arg_indices.append((col1, col2))
          # Note that this set may be different for each pair.
          no_na = self.loc[self[col1].notna() & self[col2].notna()]
          args.append(
              no_na[col1]._corr_aligned(no_na[col2], min_periods))
      def fill_matrix(*args):
        data = collections.defaultdict(dict)
        for col in columns:
          data[col][col] = 1.0
        for ix, (col1, col2) in enumerate(arg_indices):
          data[col1][col2] = data[col2][col1] = args[ix]
        return pd.DataFrame(data, columns=columns, index=columns)
      with expressions.allow_non_parallel_operations(True):
        return frame_base.DeferredFrame.wrap(
            expressions.ComputedExpression(
                'fill_matrix',
                fill_matrix,
                [arg._expr for arg in args],
                requires_partition_by=partitionings.Singleton(),
                proxy=proxy))

    else:
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'corr',
              lambda df: df.corr(method=method, min_periods=min_periods),
              [self._expr],
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def cov(self, min_periods, ddof):
    proxy = self._expr.proxy().corr()
    columns = list(proxy.columns)
    args = []
    arg_indices = []
    for col in columns:
      arg_indices.append((col, col))
      std = self[col].std(ddof)
      args.append(std.apply(lambda x: x*x, 'square'))
    for ix, col1 in enumerate(columns):
      for col2 in columns[ix+1:]:
        arg_indices.append((col1, col2))
        # Note that this set may be different for each pair.
        no_na = self.loc[self[col1].notna() & self[col2].notna()]
        args.append(no_na[col1]._cov_aligned(no_na[col2], min_periods, ddof))
    def fill_matrix(*args):
      data = collections.defaultdict(dict)
      for ix, (col1, col2) in enumerate(arg_indices):
        data[col1][col2] = data[col2][col1] = args[ix]
      return pd.DataFrame(data, columns=columns, index=columns)
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'fill_matrix',
              fill_matrix,
              [arg._expr for arg in args],
              requires_partition_by=partitionings.Singleton(),
              proxy=proxy))

  cummax = cummin = cumsum = cumprod = frame_base.wont_implement_method(
      'order-sensitive')
  diff = frame_base.wont_implement_method('order-sensitive')

  def dot(self, other):
    # We want to broadcast the right hand side to all partitions of the left.
    # This is OK, as its index must be the same size as the columns set of self,
    # so cannot be too large.
    class AsScalar(object):
      def __init__(self, value):
        self.value = value

    if isinstance(other, frame_base.DeferredFrame):
      proxy = other._expr.proxy()
      with expressions.allow_non_parallel_operations():
        side = expressions.ComputedExpression(
            'as_scalar',
            lambda df: AsScalar(df),
            [other._expr],
            requires_partition_by=partitionings.Singleton())
    else:
      proxy = pd.DataFrame(columns=range(len(other[0])))
      side = expressions.ConstantExpression(AsScalar(other))

    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'dot',
            lambda left, right: left @ right.value,
            [self._expr, side],
            requires_partition_by=partitionings.Nothing(),
            preserves_partition_by=partitionings.Index(),
            proxy=proxy))

  __matmul__ = dot

  head = tail = frame_base.wont_implement_method('order-sensitive')

  max = frame_base._agg_method('max')
  min = frame_base._agg_method('min')

  def mode(self, axis=0, *args, **kwargs):
    if axis == 1 or axis == 'columns':
      # Number of columns is max(number mode values for each row), so we can't
      # determine how many there will be before looking at the data.
      raise frame_base.WontImplementError('non-deferred column values')
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'mode',
            lambda df: df.mode(*args, **kwargs),
            [self._expr],
            #TODO(robertwb): Approximate?
            requires_partition_by=partitionings.Singleton(),
            preserves_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def dropna(self, axis, **kwargs):
    # TODO(robertwb): This is a common pattern. Generalize?
    if axis == 1 or axis == 'columns':
      requires_partition_by = partitionings.Singleton()
    else:
      requires_partition_by = partitionings.Nothing()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'dropna',
            lambda df: df.dropna(axis=axis, **kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def fillna(self, value, method, axis, **kwargs):
    if method is not None and axis in (0, 'index'):
      raise frame_base.WontImplementError('order-sensitive')
    if isinstance(value, frame_base.DeferredBase):
      value_expr = value._expr
    else:
      value_expr = expressions.ConstantExpression(value)
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'fillna',
            lambda df, value: df.fillna(
                value, method=method, axis=axis, **kwargs),
            [self._expr, value_expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Nothing()))

  isna = frame_base._elementwise_method('isna')
  notnull = notna = frame_base._elementwise_method('notna')

  items = itertuples = iterrows = iteritems = frame_base.wont_implement_method(
      'non-lazy')

  def _cols_as_temporary_index(self, cols, suffix=''):
    original_index_names = list(self._expr.proxy().index.names)
    new_index_names = [
        '__apache_beam_temp_%d_%s' % (ix, suffix)
        for (ix, _) in enumerate(original_index_names)]
    def reindex(df):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'reindex',
              lambda df:
                  df.rename_axis(index=new_index_names, copy=False)
                  .reset_index().set_index(cols),
              [df._expr],
              preserves_partition_by=partitionings.Nothing(),
              requires_partition_by=partitionings.Nothing()))
    def revert(df):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'join_restoreindex',
              lambda df:
                  df.reset_index().set_index(new_index_names)
                  .rename_axis(index=original_index_names, copy=False),
              [df._expr],
              preserves_partition_by=partitionings.Nothing(),
              requires_partition_by=partitionings.Nothing()))
    return reindex, revert

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def join(self, other, on, **kwargs):
    if on is not None:
      reindex, revert = self._cols_as_temporary_index(on)
      return revert(reindex(self).join(other, **kwargs))
    if isinstance(other, list):
      other_is_list = True
    else:
      other = [other]
      other_is_list = False
    placeholder = object()
    other_exprs = [
        df._expr for df in other if isinstance(df, frame_base.DeferredFrame)]
    const_others = [
        placeholder if isinstance(df, frame_base.DeferredFrame) else df
        for df in other]
    def fill_placeholders(values):
      values = iter(values)
      filled = [
          next(values) if df is placeholder else df for df in const_others]
      if other_is_list:
        return filled
      else:
        return filled[0]
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'join',
            lambda df, *deferred_others: df.join(
                fill_placeholders(deferred_others), **kwargs),
            [self._expr] + other_exprs,
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Index()))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def merge(
      self,
      right,
      on,
      left_on,
      right_on,
      left_index,
      right_index,
      **kwargs):
    self_proxy = self._expr.proxy()
    right_proxy = right._expr.proxy()
    # Validate with a pandas call.
    _ = self_proxy.merge(
        right_proxy,
        on=on,
        left_on=left_on,
        right_on=right_on,
        left_index=left_index,
        right_index=right_index,
        **kwargs)
    if not any([on, left_on, right_on, left_index, right_index]):
      on = [col for col in self_proxy.columns() if col in right_proxy.columns()]
    if not left_on:
      left_on = on
    elif not isinstance(left_on, list):
      left_on = [left_on]
    if not right_on:
      right_on = on
    elif not isinstance(right_on, list):
      right_on = [right_on]

    if left_index:
      indexed_left = self
    else:
      indexed_left = self.set_index(left_on, drop=False)

    if right_index:
      indexed_right = right
    else:
      indexed_right = right.set_index(right_on, drop=False)

    merged = frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'merge',
            lambda left, right: left.merge(
                right, left_index=True, right_index=True, **kwargs),
            [indexed_left._expr, indexed_right._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Index()))

    if left_index or right_index:
      return merged
    else:
      return merged.reset_index(drop=True)

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def nlargest(self, keep, **kwargs):
    if keep == 'any':
      keep = 'first'
    elif keep != 'all':
      raise frame_base.WontImplementError('order-sensitive')
    kwargs['keep'] = keep
    per_partition = expressions.ComputedExpression(
            'nlargest-per-partition',
            lambda df: df.nlargest(**kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Nothing())
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'nlargest',
              lambda df: df.nlargest(**kwargs),
              [per_partition],
              preserves_partition_by=partitionings.Singleton(),
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def nsmallest(self, keep, **kwargs):
    if keep == 'any':
      keep = 'first'
    elif keep != 'all':
      raise frame_base.WontImplementError('order-sensitive')
    kwargs['keep'] = keep
    per_partition = expressions.ComputedExpression(
            'nsmallest-per-partition',
            lambda df: df.nsmallest(**kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=partitionings.Nothing())
    with expressions.allow_non_parallel_operations(True):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'nsmallest',
              lambda df: df.nsmallest(**kwargs),
              [per_partition],
              preserves_partition_by=partitionings.Singleton(),
              requires_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.DataFrame)
  def nunique(self, **kwargs):
    if kwargs.get('axis', None) in (1, 'columns'):
      requires_partition_by = partitionings.Nothing()
    else:
      requires_partition_by = partitionings.Singleton()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'nunique',
            lambda df: df.nunique(**kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  prod = product = frame_base._agg_method('prod')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def quantile(self, axis, **kwargs):
    if axis == 1 or axis == 'columns':
      raise frame_base.WontImplementError('non-deferred column values')
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'quantile',
            lambda df: df.quantile(axis=axis, **kwargs),
            [self._expr],
            #TODO(robertwb): Approximate quantiles?
            requires_partition_by=partitionings.Singleton(),
            preserves_partition_by=partitionings.Singleton()))

  query = frame_base._elementwise_method('query')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.maybe_inplace
  def rename(self, **kwargs):
    rename_index = (
        'index' in kwargs
        or kwargs.get('axis', None) in (0, 'index')
        or ('columns' not in kwargs and 'axis' not in kwargs))
    if rename_index:
      # Technically, it's still partitioned by index, but it's no longer
      # partitioned by the hash of the index.
      preserves_partition_by = partitionings.Nothing()
    else:
      preserves_partition_by = partitionings.Singleton()
    if kwargs.get('errors', None) == 'raise' and rename_index:
      # Renaming index with checking.
      requires_partition_by = partitionings.Singleton()
      proxy = self._expr.proxy()
    else:
      requires_partition_by = partitionings.Nothing()
      proxy = None
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'rename',
            lambda df: df.rename(**kwargs),
            [self._expr],
            proxy=proxy,
            preserves_partition_by=preserves_partition_by,
            requires_partition_by=requires_partition_by))

  rename_axis = frame_base._elementwise_method('rename_axis')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def replace(self, limit, **kwargs):
    if limit is None:
      requires_partition_by = partitionings.Nothing()
    else:
      requires_partition_by = partitionings.Singleton()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'replace',
            lambda df: df.replace(limit=limit, **kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def reset_index(self, level=None, **kwargs):
    if level is not None and not isinstance(level, (tuple, list)):
      level = [level]
    if level is None or len(level) == len(self._expr.proxy().index.levels):
      # TODO: Could do distributed re-index with offsets.
      requires_partition_by = partitionings.Singleton()
    else:
      requires_partition_by = partitionings.Nothing()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'reset_index',
            lambda df: df.reset_index(level=level, **kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  round = frame_base._elementwise_method('round')
  select_dtypes = frame_base._elementwise_method('select_dtypes')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  def shift(self, axis, **kwargs):
    if 'freq' in kwargs:
      raise frame_base.WontImplementError('data-dependent')
    if axis == 1 or axis == 'columns':
      requires_partition_by = partitionings.Nothing()
    else:
      requires_partition_by = partitionings.Singleton()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'shift',
            lambda df: df.shift(axis=axis, **kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  @property
  def shape(self):
    raise frame_base.WontImplementError('scalar value')

  @frame_base.args_to_kwargs(pd.DataFrame)
  @frame_base.populate_defaults(pd.DataFrame)
  @frame_base.maybe_inplace
  def sort_values(self, axis, **kwargs):
    if axis == 1 or axis == 'columns':
      requires_partition_by = partitionings.Nothing()
    else:
      requires_partition_by = partitionings.Singleton()
    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'sort_values',
            lambda df: df.sort_values(axis=axis, **kwargs),
            [self._expr],
            preserves_partition_by=partitionings.Singleton(),
            requires_partition_by=requires_partition_by))

  stack = frame_base._elementwise_method('stack')

  sum = frame_base._agg_method('sum')

  take = frame_base.wont_implement_method('deprecated')

  to_records = to_dict = to_numpy = to_string = (
      frame_base.wont_implement_method('non-deferred value'))

  to_sparse = to_string # frame_base._elementwise_method('to_sparse')

  transform = frame_base._elementwise_method(
      'transform', restrictions={'axis': 0})

  transpose = frame_base.wont_implement_method('non-deferred column values')

  def unstack(self, *args, **kwargs):
    if self._expr.proxy().index.nlevels == 1:
      return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'unstack',
            lambda df: df.unstack(*args, **kwargs),
            [self._expr],
            requires_partition_by=partitionings.Index()))
    else:
      raise frame_base.WontImplementError('non-deferred column values')

  update = frame_base._proxy_method(
      'update',
      inplace=True,
      requires_partition_by=partitionings.Index(),
      preserves_partition_by=partitionings.Index())


for io_func in dir(io):
  if io_func.startswith('to_'):
    setattr(DeferredDataFrame, io_func, getattr(io, io_func))


for meth in ('filter', ):
  setattr(DeferredDataFrame, meth, frame_base._elementwise_method(meth))


class DeferredGroupBy(frame_base.DeferredFrame):
  def agg(self, fn):
    if not callable(fn):
      # TODO: Add support for strings in (UN)LIFTABLE_AGGREGATIONS. Test by
      # running doctests for pandas.core.groupby.generic
      raise NotImplementedError('GroupBy.agg currently only supports callable '
                                'arguments')
    return DeferredDataFrame(
        expressions.ComputedExpression(
            'agg',
            lambda df: df.agg(fn), [self._expr],
            requires_partition_by=partitionings.Index(),
            preserves_partition_by=partitionings.Singleton()))


def _liftable_agg(meth):
  name, func = frame_base.name_and_func(meth)

  def wrapper(self, *args, **kargs):
    assert isinstance(self, DeferredGroupBy)
    ungrouped = self._expr.args()[0]
    pre_agg = expressions.ComputedExpression(
        'pre_combine_' + name,
        lambda df: func(df.groupby(level=list(range(df.index.nlevels)))),
        [ungrouped],
        requires_partition_by=partitionings.Nothing(),
        preserves_partition_by=partitionings.Singleton())
    post_agg = expressions.ComputedExpression(
        'post_combine_' + name,
        lambda df: func(df.groupby(level=list(range(df.index.nlevels)))),
        [pre_agg],
        requires_partition_by=partitionings.Index(),
        preserves_partition_by=partitionings.Singleton())
    return frame_base.DeferredFrame.wrap(post_agg)

  return wrapper


def _unliftable_agg(meth):
  name, func = frame_base.name_and_func(meth)

  def wrapper(self, *args, **kargs):
    assert isinstance(self, DeferredGroupBy)
    ungrouped = self._expr.args()[0]
    post_agg = expressions.ComputedExpression(
        name,
        lambda df: func(df.groupby(level=list(range(df.index.nlevels)))),
        [ungrouped],
        requires_partition_by=partitionings.Index(),
        preserves_partition_by=partitionings.Singleton())
    return frame_base.DeferredFrame.wrap(post_agg)

  return wrapper

LIFTABLE_AGGREGATIONS = ['all', 'any', 'max', 'min', 'prod', 'size', 'sum']
UNLIFTABLE_AGGREGATIONS = ['mean', 'median', 'std', 'var']

for meth in LIFTABLE_AGGREGATIONS:
  setattr(DeferredGroupBy, meth, _liftable_agg(meth))
for meth in UNLIFTABLE_AGGREGATIONS:
  setattr(DeferredGroupBy, meth, _unliftable_agg(meth))


def _is_associative(func):
  return func in LIFTABLE_AGGREGATIONS or (
      getattr(func, '__name__', None) in LIFTABLE_AGGREGATIONS
      and func.__module__ in ('numpy', 'builtins'))


class _DeferredLoc(object):
  def __init__(self, frame):
    self._frame = frame

  def __getitem__(self, index):
    if isinstance(index, tuple):
      rows, cols = index
      return self[rows][cols]
    elif isinstance(index, list) and index and isinstance(index[0], bool):
      # Aligned by numerical index.
      raise NotImplementedError(type(index))
    elif isinstance(index, list):
      # Select rows, but behaves poorly on missing values.
      raise NotImplementedError(type(index))
    elif isinstance(index, slice):
      args = [self._frame._expr]
      func = lambda df: df.loc[index]
    elif isinstance(index, frame_base.DeferredFrame):
      args = [self._frame._expr, index._expr]
      func = lambda df, index: df.loc[index]
    elif callable(index):

      def checked_callable_index(df):
        computed_index = index(df)
        if isinstance(computed_index, tuple):
          row_index, _ = computed_index
        else:
          row_index = computed_index
        if isinstance(row_index, list) and row_index and isinstance(
            row_index[0], bool):
          raise NotImplementedError(type(row_index))
        elif not isinstance(row_index, (slice, pd.Series)):
          raise NotImplementedError(type(row_index))
        return computed_index

      args = [self._frame._expr]
      func = lambda df: df.loc[checked_callable_index]
    else:
      raise NotImplementedError(type(index))

    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'loc',
            func,
            args,
            requires_partition_by=(
                partitionings.Index()
                if len(args) > 1
                else partitionings.Nothing()),
            preserves_partition_by=partitionings.Singleton()))

class _DeferredILoc(object):
  def __init__(self, frame):
    self._frame = frame

  def __getitem__(self, index):
    if isinstance(index, tuple):
      rows, _ = index
      if rows != slice(None, None, None):
        raise frame_base.WontImplementError('order-sensitive')
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'iloc',
              lambda df: df.iloc[index],
              [self._frame._expr],
              requires_partition_by=partitionings.Nothing(),
              preserves_partition_by=partitionings.Singleton()))
    else:
      raise frame_base.WontImplementError('order-sensitive')


class _DeferredStringMethods(frame_base.DeferredBase):
  @frame_base.args_to_kwargs(pd.core.strings.StringMethods)
  @frame_base.populate_defaults(pd.core.strings.StringMethods)
  def cat(self, others, join, **kwargs):
    if others is None:
      # Concatenate series into a single String
      requires = partitionings.Singleton()
      func = lambda df: df.str.cat(join=join, **kwargs)
      args = [self._expr]

    elif (isinstance(others, frame_base.DeferredBase) or
         (isinstance(others, list) and
          all(isinstance(other, frame_base.DeferredBase) for other in others))):
      if join is None:
        raise frame_base.WontImplementError("cat with others=Series or "
                                            "others=List[Series] requires "
                                            "join to be specified.")

      if isinstance(others, frame_base.DeferredBase):
        others = [others]

      requires = partitionings.Index()
      def func(*args):
        return args[0].str.cat(others=args[1:], join=join, **kwargs)
      args = [self._expr] + [other._expr for other in others]

    else:
      raise frame_base.WontImplementError("others must be None, Series, or "
                                          "List[Series]. List[str] is not "
                                          "supported.")

    return frame_base.DeferredFrame.wrap(
        expressions.ComputedExpression(
            'cat',
            func,
            args,
            requires_partition_by=requires,
            preserves_partition_by=partitionings.Singleton()))

  @frame_base.args_to_kwargs(pd.core.strings.StringMethods)
  def repeat(self, repeats):
    if isinstance(repeats, int):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'repeat',
              lambda series: series.str.repeat(repeats),
              [self._expr],
              requires_partition_by=partitionings.Nothing(),
              preserves_partition_by=partitionings.Singleton()))
    elif isinstance(repeats, frame_base.DeferredBase):
      return frame_base.DeferredFrame.wrap(
          expressions.ComputedExpression(
              'repeat',
              lambda series, repeats_series: series.str.repeat(repeats_series),
              [self._expr, repeats._expr],
              requires_partition_by=partitionings.Index(),
              preserves_partition_by=partitionings.Singleton()))
    elif isinstance(repeats, list):
      raise frame_base.WontImplementError("repeats must be an integer or a "
                                          "Series.")


ELEMENTWISE_STRING_METHODS = [
            'capitalize',
            'casefold',
            'contains',
            'count',
            'endswith',
            'extract',
            'extractall',
            'findall',
            'get',
            'get_dummies',
            'isalpha',
            'isalnum',
            'isdecimal',
            'isdigit',
            'islower',
            'isnumeric',
            'isspace',
            'istitle',
            'isupper',
            'join',
            'len',
            'lower',
            'lstrip',
            'pad',
            'partition',
            'replace',
            'rpartition',
            'rsplit',
            'rstrip',
            'slice',
            'slice_replace',
            'split',
            'startswith',
            'strip',
            'swapcase',
            'title',
            'upper',
            'wrap',
            'zfill',
            '__getitem__',
]

def make_str_func(method):
  def func(df, *args, **kwargs):
    return getattr(df.str, method)(*args, **kwargs)
  return func

for method in ELEMENTWISE_STRING_METHODS:
  setattr(_DeferredStringMethods,
          method,
          frame_base._elementwise_method(make_str_func(method)))

for base in ['add',
             'sub',
             'mul',
             'div',
             'truediv',
             'floordiv',
             'mod',
             'pow',
             'and',
             'or']:
  for p in ['%s', 'r%s', '__%s__', '__r%s__']:
    # TODO: non-trivial level?
    name = p % base
    setattr(
        DeferredSeries,
        name,
        frame_base._elementwise_method(name, restrictions={'level': None}))
    setattr(
        DeferredDataFrame,
        name,
        frame_base._elementwise_method(name, restrictions={'level': None}))
  setattr(
      DeferredSeries,
      '__i%s__' % base,
      frame_base._elementwise_method('__i%s__' % base, inplace=True))
  setattr(
      DeferredDataFrame,
      '__i%s__' % base,
      frame_base._elementwise_method('__i%s__' % base, inplace=True))

for name in ['__lt__', '__le__', '__gt__', '__ge__', '__eq__', '__ne__']:
  setattr(DeferredSeries, name, frame_base._elementwise_method(name))
  setattr(DeferredDataFrame, name, frame_base._elementwise_method(name))
