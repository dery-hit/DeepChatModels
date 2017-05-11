"""Collection of base RNN classes and custom RNNCells.
"""

import tensorflow as tf
from tensorflow.python.util import nest
from tensorflow.python.ops import rnn_cell_impl
from chatbot.components import bot_ops
from tensorflow.contrib.rnn import RNNCell
from tensorflow.contrib.rnn import GRUCell, MultiRNNCell, LSTMStateTuple

# Required due to TensorFlow's unreliable naming across versions . . .
try:
    # r1.1
    from tensorflow.contrib.seq2seq import DynamicAttentionWrapper \
        as AttentionWrapper
    from tensorflow.contrib.seq2seq import DynamicAttentionWrapperState \
        as AttentionWrapperState
except ImportError:
    # master
    from tensorflow.contrib.seq2seq import AttentionWrapper
    from tensorflow.contrib.seq2seq import AttentionWrapperState


class Cell(RNNCell):
    """Simple wrapper class for any extensions I want to make to the
    encoder/decoder rnn cells. For now, just Dropout+GRU."""

    def __init__(self, state_size, num_layers, dropout_prob, base_cell):
        """Define the cell by composing/wrapping with tf.contrib.rnn functions.
        
        Args:
            state_size: number of units in the cell.
            num_layers: how many cells to include in the MultiRNNCell.
            dropout_prob: probability of a node being dropped.
            base_cell: (str) name of underling cell to use (e.g. 'GRUCell')
        """

        self._state_size = state_size
        self._num_layers = num_layers
        self._dropout_prob = dropout_prob
        self._base_cell = base_cell

        def single_cell():
            """Convert cell name (str) to class, and create it."""
            return getattr(tf.contrib.rnn, base_cell)(num_units=state_size)

        if num_layers == 1:
            self._cell = single_cell()
        else:
            self._cell = MultiRNNCell(
                [single_cell() for _ in range(num_layers)])

    @property
    def state_size(self):
        return self._cell.state_size

    @property
    def output_size(self):
        return self._cell.output_size

    @property
    def shape(self):
        def cell_shape():
            if "LSTM" in self._base_cell:
                return LSTMStateTuple(c=tf.TensorShape([None, self._state_size]),
                                      h=tf.TensorShape([None, self._state_size]))
            return tf.TensorShape([None, self._state_size])

        if self._num_layers == 1:
            return cell_shape()
        else:
            # tuple appears necessary for GRUCell.
            return tuple([cell_shape() for _ in range(self._num_layers)])

    def __call__(self, inputs, state, scope=None):
        """Run this RNN cell on inputs, starting from the given state.

        Args:
            inputs: `2-D` tensor with shape `[batch_size x input_size]`.
            state: Either 2D Tensor or tuple of 2D tensors, determined by cases:
                - `self.state_size` is int: `2-D Tensor` with shape
                    `[batch_size x self.state_size]`.
                - `self.state_size` is tuple: tuple with shapes
                    `[batch_size x s] for s in self.state_size`.
            scope: VariableScope for the created subgraph; 
                defaults to class name.

        Returns:
            A pair containing:
            - Output: 2D tensor with shape [batch_size x self.output_size].
            - New state: Either a single `2-D` tensor, or a tuple of tensors 
                matching the arity and shapes of `state`.
        """
        output, new_state = self._cell(inputs, state, scope)
        output = tf.layers.dropout(output, rate=self._dropout_prob, name="dropout")
        return output, new_state


class RNN(object):
    """Base class for encoders/decoders. Has simple instance attributes and
    an RNNCell object and getter.
    """

    def __init__(self,
                 state_size,
                 embed_size,
                 dropout_prob,
                 num_layers,
                 base_cell="GRUCell",
                 state_wrapper=None):
        """
        Args:
            state_size: number of units in underlying rnn cell.
            embed_size: dimension size of word-embedding space.
            dropout_prob: probability of a node being dropped.
            num_layers: how many cells to include in the MultiRNNCell.
            base_cell: (str) name of underling cell to use (e.g. 'GRUCell')
            state_wrapper: allow states to store their wrapper class. See the
                wrapper method docstring below for more info.
        """
        self.state_size = state_size
        self.embed_size = embed_size
        self.num_layers = num_layers
        self.dropout_prob = dropout_prob
        self.base_cell = base_cell
        self._wrapper = state_wrapper

    def get_cell(self, name, **kwargs):
        """Returns a cell instance, defined by its name scope."""
        with tf.name_scope(name, "get_cell"):
            cell = Cell(state_size=self.state_size,
                        num_layers=self.num_layers,
                        dropout_prob=self.dropout_prob,
                        base_cell=self.base_cell)
            if kwargs.get('attn') is None:
                return cell

            cell = MyAttentionWrapper(cell=cell,
                                      attention_mechanism=kwargs['attn'],
                                      output_attention=kwargs.get('output_attention', False),
                                      attention_layer_size=kwargs['attention_size'])
            return cell

    def wrapper(self, state):
        """Some RNN states are wrapped in namedtuples. 
        (TensorFlow decision, definitely not mine...). 
        
        This is here for derived classes to specify their wrapper state. 
        Some examples: LSTMStateTuple and AttentionWrapperState.
        
        Args:
            state: tensor state tuple, will be unpacked into the wrapper tuple.
        """
        if self._wrapper is None:
            return state
        else:
            return self._wrapper(*state)

    def __call__(self, *args):
        raise NotImplemented


class MyAttentionWrapper(RNNCell):
    """Wraps another `RNNCell` with attention.
    Mostly the same as tf.contrib.seq2seq.AttentionWrapper, but with less 
    headaches (for me) and custom tweaks to fit with the project better.
    """

    def __init__(self,
                 cell,
                 attention_mechanism,
                 attention_layer_size=None,
                 output_attention=False,
                 name=None):
        """Construct the `AttentionWrapper`.

        Args:
            cell: An instance of `RNNCell`.
            attention_mechanism: An instance of `AttentionMechanism`.
            attention_layer_size: Python integer, the depth of the attention (output)
                layer. If None (default), use the context as attention at each time
                step. Otherwise, feed the context and cell output into the attention
                layer to generate attention at each time step.
            output_attention: Python bool.  If `True` (default), the output at each
                time step is the attention value (Luong-style). If `False`, 
                the output at each time step is the output of `cell` 
                (Bhadanau-style). In both cases, the `attention` tensor is
                propagated to the next time step via the state and is used there.
                This flag only controls whether the attention mechanism is propagated
                up to the next cell in an RNN stack or to the top RNN output.
            name: Name to use when creating ops.
        """
        super(MyAttentionWrapper, self).__init__(name=name)

        # Assume that 'cell' is an instance of the custom 'Cell' class above.
        self._base_cell = cell._base_cell
        self._num_layers = cell._num_layers
        self._state_size = cell._state_size

        def cell_input_fn(inputs, attention):
            return tf.concat([inputs, attention], -1)

        if attention_layer_size is not None:
            self._attention_layer = tf.contrib.keras.layers.Dense(attention_layer_size,
                                        name="attention_layer",
                                        use_bias=False)
            self._attention_size = attention_layer_size
        else:
            self._attention_layer = None
            self._attention_size = attention_mechanism.values.get_shape()[-1].value

        self._cell = cell
        self._attention_mechanism = attention_mechanism
        self._cell_input_fn = cell_input_fn
        self._output_attention = output_attention
        self._alignment_history = False
        with tf.name_scope(name, "AttentionWrapperInit"):
            self._initial_cell_state = None

    @property
    def output_size(self):
        if self._output_attention:
            return self._attention_size
        else:
            return self._cell.output_size

    @property
    def state_size(self):
        return AttentionWrapperState(cell_state=self._cell.state_size,
                                     time=tf.TensorShape([]),
                                     attention=self._attention_size,
                                     alignment_history=())

    def zero_state(self, batch_size, dtype=tf.float32):
        with tf.name_scope(type(self).__name__ + "ZeroState", values=[batch_size]):
            cell_state = self._cell.zero_state(batch_size, dtype)
            alignment_history = ()
            _zero_state_tensors = rnn_cell_impl._zero_state_tensors
            return AttentionWrapperState(
                cell_state=cell_state,
                time=tf.zeros([], dtype=tf.int32),
                attention=_zero_state_tensors(self._attention_size, batch_size, dtype),
                alignment_history=alignment_history)

    def initialized_state(self, state, batch_size, dtype=tf.float32):
        _zero_state_tensors = rnn_cell_impl._zero_state_tensors
        return AttentionWrapperState(
            cell_state=state,
            time=tf.zeros([], dtype=tf.int32),
            attention=_zero_state_tensors(self._attention_size, batch_size, dtype),
            alignment_history=())

    def call(self, inputs, state):
        """Perform a step of attention-wrapped RNN.
       
        Args:
            inputs: (Possibly nested tuple of) Tensor, the input at this time step.
            state: An instance of `AttentionWrapperState` containing
            tensors from the previous time step.
            
        Returns:
            A tuple `(attention_or_cell_output, next_state)`, where:
        """

        # Step 1: Calculate the true inputs to the cell based on the
        # previous attention value.
        cell_inputs = self._cell_input_fn(inputs, state.attention)
        cell_state = state.cell_state

        # Step 2: Call the wrapped `cell` with input and prev state.
        cell_output, next_cell_state = self._cell(cell_inputs, cell_state)

        # Step 3: Compute the scores with either Bahdanau/Luong.
        att_query = tf.contrib.layers.fully_connected(
            inputs=cell_output,
            num_outputs=self._attention_size,
            activation_fn=None)
        score = self._attention_mechanism(att_query)

        # Step 4: Calculate the alignments by passing the score through softmax.
        alignments = tf.nn.softmax(score)

        # Reshape from [batch_size, memory_time] to [batch_size, 1, memory_time]
        expanded_alignments = tf.expand_dims(alignments, 1)

        # Step 5: Calculate the context vector as the inner product between the
        # alignments and the attention_mechanism's values (memory).
        # - alignments shape:
        #     [B, 1, memory_time]
        # - attention_mechanism.values shape:
        #     [B, memory_time, attention_mechanism.num_units]
        # - output shape:
        #     [B, 1, attention_mechanism.num_units].
        context = tf.matmul(expanded_alignments,
                            self._attention_mechanism.values)

        # we then squeeze out the singleton dim.
        context = tf.squeeze(context, [1])

        # Step 6: Calculate the attention output by concatenating the cell output
        #and context through the attention layer (a linear layer with
        # `attention_size` outputs).
        if self._attention_layer is not None:
          attention = self._attention_layer(
              tf.concat([cell_output, context], 1))
        else:
          attention = context

        alignment_history = ()

        next_state = AttentionWrapperState(
            cell_state=next_cell_state,
            attention=attention,
            time=state.time + 1,
            alignment_history=alignment_history)

        if self._output_attention:
            return attention, next_state
        else:
            return cell_output, next_state


    @property
    def shape(self):
        """The hoops you make me jump through, TensorFlow..."""
        if self._num_layers == 1:
            return AttentionWrapperState(
                cell_state=tf.TensorShape([None, self._state_size]),
                attention=tf.TensorShape([None, self._attention_size]),
                time=tf.TensorShape(None),
                alignment_history=())
            #return [tf.TensorShape([None, self._state_size]),
            #        tf.TensorShape([None, self._attention_size]),
            #        tf.TensorShape(None), ()]
        else:
            def layer_shape():
                return tf.TensorShape([None, self._state_size])
            return AttentionWrapperState(
                cell_state=tuple([layer_shape() for _ in range(self._num_layers)]),
                attention=tf.TensorShape([None, self._attention_size]),
                time=tf.TensorShape(None),
                alignment_history=())
            #return [tuple([layer_shape() for _ in range(self._num_layers)]),
            #        tf.TensorShape([None, self._attention_size]),
            #        tf.TensorShape(None), ()]


class BasicRNNCell(RNNCell):
    """Same as tf.contrib.rnn.BasicRNNCell, rewritten for clarity.

    For example, many TF implementations have leftover code debt from past 
    versions, so I wanted to show what is actually going on, with the fluff 
    removed. Also, I've removed generally accepted values from parameters/args 
    in favor of just setting them.
    """

    def __init__(self, num_units, reuse=None):
        self._num_units = num_units
        self._reuse = reuse

    @property
    def state_size(self):
        return self._num_units

    @property
    def output_size(self):
        return self._num_units

    def __call__(self, inputs, state, scope=None):
        """Most basic RNN. Define as:
            output = new_state = act(W * input + U * state + B).
        """
        output = tf.tanh(bot_ops.linear_map(
            args=[inputs, state],
            output_size=self._num_units,
            bias=True))
        return output, output


