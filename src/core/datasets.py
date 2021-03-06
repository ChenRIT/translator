#coding:utf-8
import tensorflow as tf
import sys, re, random, itertools, collections, os
import numpy as np
import pandas as pd
from nltk.tokenize import sent_tokenize, word_tokenize
from core.vocabularies import _BOS, BOS_ID, _PAD, PAD_ID, _NUM
from utils import evaluation, tf_utils, common

def find_entry(target_labels):
  """
  Args:
    - target_labels : A string with the following format.  
      (e.g. "(30|1|60|1|$|item):(100|0|-|-|$|hour)")
  """
  try:
    labels = [x.strip() for x in re.search('\((.+)\)', target_labels.split(':')[0]).group(1).split('|')]
  except:
    print target_labels
    exit(1)
  return labels


class DatasetBase(object):
  pass

class PriceDataset(DatasetBase):
  NONE = '-'

  @common.timewatch()
  def __init__(self, path, vocab, num_train_data=0):
    sys.stderr.write('Loading dataset from %s ...\n' % (path))
    data = pd.read_csv(path)
    if num_train_data:
      data = data[:num_train_data]
    self.vocab = vocab
    self.tokenizer = vocab.tokenizer

    text_data = data['sentence']

    # For copying, keep unnormalized sentences too.
    self.original_sources = [[_BOS] + self.tokenizer(l, normalize_digits=False) for l in text_data]
    self.indexs = data['index'].values
    self.sources = [[_BOS] + self.tokenizer(l) for l in text_data]

    label_data = data['label']
    targets = [[self.tokenizer(x, normalize_digits=False) for x in find_entry(l)] for l in label_data]

    lowerbounds, l_equals, upperbounds, u_equals, currencies, rates = zip(*targets)
    self.targets = [lowerbounds, upperbounds, currencies, rates] 
    self.targets = list(zip(*self.targets))
    self.targets_name = ['LB', 'UB', 'Currency', 'Rate']
    
  def get_batch(self, batch_size, 
                input_max_len=None, output_max_len=None, shuffle=False):
    sources, targets = self.symbolized
    if input_max_len:
      paired = [(s,t) for s,t in zip(sources, targets) if not len(s) > input_max_len ]
      sources, targets = list(zip(*paired))

    sources =  tf.keras.preprocessing.sequence.pad_sequences(sources, maxlen=input_max_len, padding='post', truncating='post', value=PAD_ID)
    targets = list(zip(*targets)) # to column-major. (for padding)
    targets = [tf.keras.preprocessing.sequence.pad_sequences(targets_by_column, maxlen=output_max_len, padding='post', truncating='post', value=PAD_ID) for targets_by_column in targets]
    targets = list(zip(*targets)) # to idx-major. (for shuffling)

    data = [tuple(x) for x in zip(sources, targets, self.original_sources, self.targets)]

    if shuffle:
      random.shuffle(data)
    for i, b in itertools.groupby(enumerate(data), 
                                  lambda x: x[0] // (batch_size)):
      batch = [x[1] for x in b]
      b_sources, b_targets, b_ori_sources, b_ori_targets = zip(*batch)
      b_targets = list(zip(*b_targets)) # to column-major.
      yield common.dotDict({
        'sources': np.array(b_sources),
        'targets': [np.array(t) for t in b_targets],
        'original_sources': b_ori_sources,
        'original_targets': b_ori_targets,
      })
  
  def create_demo_batch(self, text, output_max_len):
    source = [self.vocab.tokens2ids(text)]
    targets = [[[0] for _ in self.targets_name]]
    targets = list(zip(*targets)) # to column-major. (for padding)
    source =  tf.keras.preprocessing.sequence.pad_sequences(source, padding='post', truncating='post', value=PAD_ID)
    targets = [tf.keras.preprocessing.sequence.pad_sequences(targets_by_column, maxlen=output_max_len, padding='post', truncating='post', value=PAD_ID) for targets_by_column in targets]
    
    yield common.dotDict({
      'sources': np.array(source),
      'targets': [np.array(t) for t in targets],
    })

  @property 
  def raw_data(self):
    return self.indexs, self.original_sources, self.targets

  @property
  def symbolized(self):
    # Find the indice of the tokens in a source sentence which was copied as a target label.
    targets = [[[o.index(x) for x in tt if x != self.NONE and x in o ] for tt in t] for o, t in zip(self.original_sources, self.targets)]
    #targets = list(zip(*targets))
    sources = [self.vocab.tokens2ids(s) for s in self.sources]
    #return list(zip(sources, targets))
    return sources, targets

  # @property
  # def tensorized(self):
  #   sources, targets = self.symbolized
  #   sources =  tf.keras.preprocessing.sequence.pad_sequences(sources, maxlen=config.input_max_len+1, padding='post', truncating='post', value=BOS_ID)
  #   targets =  tf.keras.preprocessing.sequence.pad_sequences(targets, maxlen=config.output_max_len, padding='post', truncating='post', value=BOS_ID)
  #   sources = tf.data.Dataset.from_tensor_slices(sources)
  #   targets = tf.data.Dataset.from_tensor_slices(targets)
  #   dataset = tf.data.Dataset.zip((sources, targets))
  #   return dataset

  def show_results(self, original_sources, targets, predictions, 
                   verbose=True, prediction_is_index=True, 
                   target_path_prefix=None, output_types=None):
    test_inputs_tokens, _ = self.symbolized
    golds = []
    preds = []
    try:
      assert len(original_sources) == len(targets) == len(predictions)
    except:
      print len(original_sources), len(targets), len(predictions)
      exit(1)

    inputs = [] # Original texts.
    token_inputs = [] # Normalized texts with _UNK.
    for i, (raw_s, raw_t, p) in enumerate(zip(original_sources, targets, predictions)):
      token_inp = self.vocab.ids2tokens(test_inputs_tokens[i])
      if prediction_is_index:
        inp, gold, pred = self.ids_to_tokens(raw_s, raw_t, p)
      else:
        inp, gold, _ = self.ids_to_tokens(raw_s, raw_t, p)
        pred = [x.strip() for x in p]
      inputs.append(inp)
      token_inputs.append(token_inp)
      golds.append(gold)
      preds.append(pred)

    # Functions to analyze results of complicated targets.
    # (name, (func1, func2))
    # func1 : A function to decide whether an example will be imported in the results. (True or False)
    # func2 : A function to decide an extraction to be a success or not.

    lower_upper_success = lambda g, p: (g[0], g[1]) == (p[0], p[1])
    all_success = lambda g, p: tuple(g) == tuple(p)
    conditions = [
      ('all', (lambda g: True, all_success)),
      ('range', (lambda g: g[0] != g[1], lower_upper_success)),
      ('multi', (lambda g: len(g[0].split(' ')) > 1 or len(g[1].split(' ')) > 1, 
                 lower_upper_success)),
      ('less', (lambda g: g[0] == '-' and g[1] != '-', lower_upper_success)),
      ('more', (lambda g: g[0] != '-' and g[1] == '-', lower_upper_success)),
    ]
    if output_types is not None:
      conditions = [c for c in conditions if c[0] in output_types]

    dfs = []
    summaries = []
    for (name, cf) in conditions:
      with open("%s.%s" % (target_path_prefix, name), 'w') as f:
        sys.stdout = f
        df, summary = self.summarize(inputs, token_inputs, golds, preds, cf)
        sys.stdout = sys.__stdout__
      dfs.append(df)
      summaries.append(summary)
    return dfs[0], summaries[0]

  def summarize(self, inputs, token_inputs, _golds, _preds, cf):
    '''
    inputs: 2D array of string in original sources. [len(data), max_source_length]
    token_inputs: 2D array of string. Tokens not in the vocabulary are converted into _UNK. [len(data), max_source_length]
    _golds: 2D array of string. [len(data), num_columns]
    _preds: 2D array of string. [len(data), num_columns]
    cf: Tuple of two functions. cf[0] is for the condition whether it includes a line of test into the summary, and cf[1] is to decide whether a pair of tuple (gold, prediction) for a line of testing is successful of not. 
    '''
    golds, preds = [], []
    for i, (inp, token_inp, g, p) in enumerate(zip(inputs, token_inputs, _golds, _preds)):
      if not cf[0](g):
        continue
      golds.append(g)
      preds.append(p)
      is_success = cf[1](g, p)
      succ_or_fail = 'EM_Success' if is_success else "EM_Failure"
      sys.stdout.write('<%d> (%s)\n' % (i, succ_or_fail))
      sys.stdout.write('Test input       :\t%s\n' % (' '.join(inp)))
      sys.stdout.write('Test input (unk) :\t%s\n' % (' '.join(token_inp)))
      sys.stdout.write('Human label      :\t%s\n' % (' | '.join(g)))
      sys.stdout.write('Test prediction  :\t%s\n' % (' | '.join(p)))

    EM = evaluation.exact_match(golds, preds)
    precisions, recalls = evaluation.precision_recall(golds, preds)

    res = {'Metrics': ['EM accuracy', 'Precision', 'Recall']}
    for col, col_name in zip(zip(EM, precisions, recalls), self.targets_name):
      res[col_name] = col

    df = pd.DataFrame(res)
    df = df.ix[:,['Metrics'] + self.targets_name].set_index('Metrics')

    # Make summary for Tensorboard.
    column_names = [x for x in df]
    row_names = [x for x in df.index]
    summary_dict = {}
    for row_name, row in zip(row_names, df.values.tolist()):
      for column_name, val in zip(column_names, row):
        k = "%s/%s" % (row_name, column_name)
        summary_dict[k] = val
    summary = tf_utils.make_summary(summary_dict)
    print ''
    print df

    return df, summary

  def ids_to_tokens(self, s_tokens, t_tokens, p_idxs):
    outs = []
    preds = []
    inp = [x for x in s_tokens if x not in [_BOS, _PAD]]
    if t_tokens is not None:
      for tt in t_tokens:
        out = ' '.join([x for x in tt if x not in [_BOS, _PAD]])
        outs.append(out)
    for pp in p_idxs:
      pred =  [s_tokens[k] for k in pp if len(s_tokens) > k]
      pred = ' '.join([x for x in pred if x not in [_BOS, _PAD]])
      if not pred:
        pred = self.NONE
      preds.append(pred)
    return inp, outs, preds


class NumSymbolizePriceDataset(PriceDataset):
  def __init__(self, path, vocab, num_train_data=0):
    PriceDataset.__init__(self, path, vocab, num_train_data)
    self.vocab.add2vocab(_NUM)
    self.pos = common.get_pos(self.original_sources, output_path=path)
    assert len(self.pos) == len(self.sources)
    self.original_sources, self.targets, self.num_indices = self.concat_numbers(
      self.original_sources, self.targets, self.pos 
    )
    self.sources = self.original_sources

  @property
  def symbolized(self):
    # Find the indice of the tokens in a source sentence which was copied as a target label.
    targets = [[[o.index(x) for x in tt if x != self.NONE and x in o ] for tt in t] for o, t in zip(self.original_sources, self.targets)]
    sources = [self.vocab.tokens2ids([x if i not in idx else _NUM for i, x in enumerate(s)]) for s, idx in zip(self.sources, self.num_indices)]
    return sources, targets

  def concat_numbers(self, _sources, _targets, _pos):
    # Concatenate consecutive CDs in sources and targets by '|'.
    # num_indices shows the concatenated tokens and they will be normalized into '_NUM' when symbolizing.
    delim = '|'
    sources = []
    targets = []
    num_indices = []
    for s, t, p in zip(_sources, _targets, _pos):
      assert len(s) == len(p)
      new_s = []
      tmp = []
      idx = []
      for i in xrange(len(s)):
        if p[i] == 'CD' and s[i] not in ['(', ')', '-']:
          tmp.append(s[i])
        else:
          if len(tmp) > 0:
            concated_num = delim.join(tmp)
            new_s.append(concated_num)
            tmp = []
            idx.append(new_s.index(concated_num))
          new_s.append(s[i])
      if len(tmp) > 0:
        new_s.append(delim.join(tmp))
        idx.append(i-1)
      new_t = ([delim.join(t[0])], [delim.join(t[1])], t[2], t[3])
      sources.append(new_s)
      targets.append(new_t)
      num_indices.append(idx)
    return sources, targets, num_indices

