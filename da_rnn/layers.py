import tensorflow as tf

from tensorflow.keras.layers import (
    Layer,
    LSTM,
    Dense,
    RepeatVector,
    Permute
)


# Notation Convention
# ```
# Variable_{subscript}__{superscript}
# ```

class InputAttention(Layer):
    def __init__(self, T):
        """
        Calculates the attention weight alpha_t__k

        Args:
            T (int): the number of driving (exogenous) series
        """

        super().__init__(name='input_attention')

        self.W = Dense(T)
        self.U = Dense(T)
        self.v = Dense(1)

    def call(
        self,
        hidden_state,
        cell_state,
        X
    ):
        """
        Args:
            hidden_state: hidden state of shape (batch_size, m)
            cell_state: cell state of shape (batch_size, m)
            X: the n driving (exogenous) series of shape (batch_size, T, n)

        Returns:
            The attention weights (alpha_t) at time t, i.e.
            (a_t__1, a_t__2, ..., a_t__n)
        """

        n = X.shape[2]

        # Equation 8:
        e = self.v(
            tf.math.tanh(
                self.W(
                    # [h_t-1; s_t-1]
                    RepeatVector(n)(
                        tf.concat([hidden_state, cell_state], axis=-1)
                        # -> (batch_size, m * 2)
                    )
                    # -> (batch_size, n, m * 2)
                )
                # -> (batch_size, n, T)

                + self.U(
                    Permute((2, 1))(X)
                )
                # -> (batch_size, n, T)
            )
            # -> (batch_size, n, T)
        )
        # -> (batch_size, n, 1)

        # Equation: 9
        return tf.nn.softmax(
            Permute((2, 1))(e)
            # -> (batch_size, 1, n)
        )
        # -> (batch_size, 1, n)


class EncoderInput(Layer):
    T: int

    def __init__(
        self,
        T: int,
        m: int
    ):
        """
        Generates the new input x_tilde at time t for encoder

        Args:
            T (int): the length (time steps) of window size
            m (int): the number of the encoder hidden states
        """

        super().__init__(name='encoder_input')

        self.T = T

        self.input_lstm = LSTM(m, return_state=True)
        self.input_attention = InputAttention(T)

        self.initial_state = None

    def call(
        self,
        X,
        h0,
        s0
    ):
        """
        Args:
            X: driving series of length T (batch_size, T, n)
            n: the number of features

        Returns:
            (x_tilde_1, ..., x_tilde_t, ..., x_tilde_T)
        """

        alpha_weights = tf.TensorArray(tf.float32, self.T)

        for t in range(self.T):
            x = X[:, t, :][:, None, :]
            # -> (batch_size, n) -> (batch_size, 1, n)

            hidden_state, cell_state = self.input_lstm(
                x,
                initial_state=[h0, s0]
            )

            alpha_t = self.input_attention(hidden_state, cell_state, X)
            # -> (batch_size, 1, n)

            alpha_weights = alpha_weights.write(
                t,
                alpha_t
            )

        # Equation 10
        return tf.multiply(X, alpha_weights.stack())
        # -> (batch_size, T, n)


class TemporalAttention(Layer):
    def __init__(self, m: int):
        """
        Calculates the attention weights::

            beta_t = (beta_t__1, ..., beta_t__i, ..., beta_t__T) (1 <= i <= T)

        for each encoder hidden state h_t at the time step t

        Args:
            m (int): the number of the encoder hidden states
        """

        super().__init__(name='temporal_attention')

        self.W = Dense(m)
        self.U = Dense(m)
        self.v = Dense(1)

    def call(
        self,
        hidden_state,
        cell_state,
        encoder_h
    ):
        """
        Args:
            hidden_state: hidden state `d` of shape (batch_size, p)
            cell_state: cell state `s` of shape (batch_size, p)
            encoder_h: the encoder hidden states (batch_size, T, m)

        Returns:
            The attention weights for encoder hidden states (beta_t)
        """

        # Equation 12
        l = self.v(
            tf.math.tanh(
                self.W(
                    RepeatVector(encoder_h.shape[1])(
                        tf.concat([hidden_state, cell_state], axis=-1)
                        # -> (batch_size, p * 2)
                    )
                    # -> (batch_size, T, p * 2)
                )
                # -> (batch_size, T, m)
                + self.U(encoder_h)
            )
            # -> (batch_size, T, m)
        )
        # -> (batch_size, T, 1)

        # Equation 13
        return tf.nn.softmax(l, axis=1)
        # -> (batch_size, T, 1)


class Decoder(Layer):
    def __init__(self, T, p, m):
        """
        Calculates y_hat_T
        """

        super().__init__(name='decoder')

        self.T = T

        self.temp_attention = TemporalAttention(m)
        self.dense = Dense(1)
        self.decoder_lstm = LSTM(p, return_state=True)
        self.encoder_lstm_units = m

        self.dense_Wb = Dense(p)
        self.dense_vb = Dense(1)

    def call(
        self,
        data,
        encoder_h,
        h0,
        s0
    ):
        """
        Args:
            data: decoder data of shape (batch_size, T - 1, 1)
            encoder_h: encoder hidden states of shape (batch_size, T, m)
            h0: initial decoder hidden state
            s0: initial decoder cell state
        """

        hidden_state = None
        batch_size = encoder_h.shape[0]

        # c in the paper
        context_vector = tf.zeros((batch_size, 1, self.encoder_lstm_units))
        # -> (batch_size, 1, m)

        for t in range(self.T - 1):
            x = data[:, t, :][:, None, :]
            # -> (batch_size, 1, 1)

            # Equation 15
            y_tilde = self.dense(
                tf.concat([x, context_vector], axis=-1)
                # -> (batch_size, 1, m + 1)
            )
            # -> (batch_size, 1, 1)

            # Equation 16
            hidden_state, cell_state = self.decoder_lstm(
                y_tilde,
                initial_state=[h0, s0]
            )
            # -> (batch_size, p)

            beta_t = self.temp_attention(
                hidden_state,
                cell_state,
                encoder_h
            )  # batch, T, 1

            # Equation 14
            context_vector = tf.matmul(
                beta_t, encoder_h, transpose_a=True
            )
            # -> (batch_size, 1, m)

        concatenated = tf.concat(
            [hidden_state[:, None, :], context_vector], axis=-1
        )
        # -> (batch_size, 1, m + p)

        # Equation 22
        return self.dense_Wb(
            self.dense_vb(concatenated)
        )