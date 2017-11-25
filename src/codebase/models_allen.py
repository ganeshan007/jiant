import pdb
import time
import logging as log
from typing import Any, Dict, List, Optional

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.functional as F

from allennlp.common import Params
from allennlp.common.checks import ConfigurationError
from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Highway, MatrixAttention
from allennlp.modules import Seq2SeqEncoder, SimilarityFunction, TimeDistributed, TextFieldEmbedder
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import BooleanAccuracy, CategoricalAccuracy

logger = log.getLogger(__name__)  # pylint: disable=invalid-name

class MultiTaskModel(nn.Module):
    '''
    Playing around designing a class
    '''

    def __init__(self, sent_encoder, pair_encoder, pair_enc_type):
        '''

        Args:
        '''
        super(MultiTaskModel, self).__init__()
        self.sent_encoder = sent_encoder
        self.pair_encoder = pair_encoder
        assert pair_enc_type in ['bidaf', 'simple']
        self.pair_enc_type = pair_enc_type
        self.pred_layers = {}
        self.scorers = {}
        self.losses = {}

    def build_classifier(self, task, classifier_type,
                         input_dim, hid_dim, dropout):
        '''
        Build a task specific prediction layer and register it
        '''
        if classifier_type == 'log_reg':
            layer = nn.Linear(input_dim, task.n_classes)
        elif classifier_type == 'mlp':
            layer = nn.Sequential(nn.Dropout(p=dropout),
                    nn.Linear(input_dim, hid_dim), nn.Tanh(),
                    nn.Dropout(p=dropout),
                    nn.Linear(hid_dim, task.n_classes))
        elif classifier_type == 'fancy_mlp':
            layer = nn.Sequential(nn.Dropout(p=dropout),
                    nn.Linear(task.input_dim, hid_dim), nn.Tanh(),
                    nn.Dropout(p=dropout), nn.Linear(hid_dim, hid_dim),
                    nn.Tanh(), nn.Dropout(p=dropout),
                    nn.Linear(hid_dim, task.n_classes))
        else:
            raise ValueError("Unrecognized classifier!")

        self.pred_layers[task.name] = layer
        self.add_module('%s_pred_layer' % task.name, layer)
        '''
        if isinstance(task, STSTask):
            self.scorer[task.name] = Average()
            self.losses[task.name] = nn.MSELoss()
        else:
            self.scorer[task.name] = CategoricalAccuracy()
            self.losses[task.name] = nn.CrossEntropyLoss()
        '''

    #def forward(self, pred_layer=None, pair_input=1, scorer=None,
    def forward(self, task=None,
                input1=None, input2=None, label=None):
        '''
        Predict through model and task-specific prediction layer

        Args:
            - inputs (tuple(TODO))
            - pred_layer (nn.Module)
            - pair_input (int)

        Returns:
            - logits (TODO)
        '''
        pair_input = task.pair_input
        pred_layer = self.pred_layers[task.name]
        scorer = task.scorer
        if pair_input:
            if self.pair_enc_type == 'bidaf':
                pair_embs = self.pair_encoder(input1, input2)
                pair_emb, _ = pair_embs.max(1)
                logits = pred_layer(pair_emb)
            elif self.pair_enc_type == 'simple':
                pair_emb = self.pair_encoder(input1, input2)
                logits = pred_layer(pair_emb)
        else:
            #sent_embs = self.sent_encoder(input1)
            #sent_emb, _ = sent_embs.max(1)
            sent_emb = self.sent_encoder(input1)
            logits = pred_layer(sent_emb)
        out = {'logits': logits}
        #pdb.set_trace()
        if label is not None:
            if hasattr(task, 'loss') and task.loss is not None:
                loss = task.loss(logits, label.squeeze(-1))
                scorer(loss.data.cpu()[0])
            else:
                loss = F.cross_entropy(logits, label.squeeze(-1))
                scorer(logits, label.squeeze(-1))
            out['loss'] = loss
        return out

class HeadlessPairEncoder(Model):
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 dropout: float = 0.2,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(HeadlessPairEncoder, self).__init__(vocab)#, regularizer)

        self._text_field_embedder = text_field_embedder
        self._highway_layer = TimeDistributed(Highway(text_field_embedder.get_output_dim(), num_highway_layers))
        self._phrase_layer = phrase_layer

        encoding_dim = phrase_layer.get_output_dim()
        self.output_dim = encoding_dim

        if text_field_embedder.get_output_dim() != \
                phrase_layer.get_input_dim():
            raise ConfigurationError("The output dimension of the "
                                     "text_field_embedder "
                                     "(embedding_dim + char_cnn) "
                                     "must match the input "
                                     "dimension of the phrase_encoder. "
                                     "Found {} and {} "
                                     "respectively.".format(text_field_embedder.get_output_dim(), phrase_layer.get_input_dim()))
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)

    def forward(self, question, passage):
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passage : Dict[str, torch.LongTensor]
            From a ``TextField``.  The model assumes that this passage contains the answer to the
            question, and predicts the beginning and ending positions of the answer within the
            passage.

        Returns
        -------
        pair_rep : torch.FloatTensor?
            Tensor representing the final output of the BiDAF model
            to be plugged into the next module

        """
        embedded_question = self._highway_layer(self._text_field_embedder(question))
        embedded_passage = self._highway_layer(self._text_field_embedder(passage))
        batch_size = embedded_question.size(0)
        passage_length = embedded_passage.size(1)
        question_mask = util.get_text_field_mask(question).float()
        passage_mask = util.get_text_field_mask(passage).float()
        question_lstm_mask = question_mask if self._mask_lstms else None
        passage_lstm_mask = passage_mask if self._mask_lstms else None

        encoded_question = self._dropout(self._phrase_layer(embedded_question, question_lstm_mask))
        encoded_passage = self._dropout(self._phrase_layer(embedded_passage, passage_lstm_mask))

        '''
        Want to kill padding terms by making very negative
            - pad terms are already 0's
            - get inverse mask and send 1 -> big negative number
            - add negative mask
        ''' 
        passage_lstm_mask[passage_lstm_mask == 0] = -1e3
        passage_lstm_mask[passage_lstm_mask == 1] = 0
        passage_lstm_mask = passage_lstm_mask.unsqueeze(dim=-1)
        question_lstm_mask[question_lstm_mask == 0] = -1e3
        question_lstm_mask[question_lstm_mask == 1] = 0
        question_lstm_mask = question_lstm_mask.unsqueeze(dim=-1)

        encoded_question, _ = (encoded_question + question_lstm_mask).max(1)
        encoded_passage, _ = (encoded_passage + passage_lstm_mask).max(1)

        return torch.cat([encoded_question, encoded_passage,
                          torch.abs(encoded_question - encoded_passage),
                          encoded_question * encoded_passage], 1)


class HeadlessSentEncoder(Model):
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 dropout: float = 0.2,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(HeadlessSentEncoder, self).__init__(vocab)#, regularizer)

        self._text_field_embedder = text_field_embedder
        self._highway_layer = TimeDistributed(Highway(text_field_embedder.get_output_dim(), num_highway_layers))
        self._phrase_layer = phrase_layer

        encoding_dim = phrase_layer.get_output_dim()
        self.output_dim = encoding_dim

        if text_field_embedder.get_output_dim() != \
                phrase_layer.get_input_dim():
            raise ConfigurationError("The output dimension of the "
                                     "text_field_embedder "
                                     "(embedding_dim + char_cnn) "
                                     "must match the input "
                                     "dimension of the phrase_encoder. "
                                     "Found {} and {} "
                                     "respectively.".format(text_field_embedder.get_output_dim(), phrase_layer.get_input_dim()))
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)

    def forward(self, question):
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passage : Dict[str, torch.LongTensor]
            From a ``TextField``.  The model assumes that this passage contains the answer to the
            question, and predicts the beginning and ending positions of the answer within the
            passage.

        Returns
        -------
        pair_rep : torch.FloatTensor?
            Tensor representing the final output of the BiDAF model
            to be plugged into the next module

        """
        embedded_question = self._highway_layer(self._text_field_embedder(question))
        question_mask = util.get_text_field_mask(question).float()
        question_lstm_mask = question_mask if self._mask_lstms else None

        encoded_question = self._dropout(self._phrase_layer(embedded_question, question_lstm_mask))
        question_lstm_mask[question_lstm_mask == 0] = -1e3
        question_lstm_mask[question_lstm_mask == 1] = 0
        question_lstm_mask = question_lstm_mask.unsqueeze(dim=-1)

        return (encoded_question + question_lstm_mask).max(1)[0]



@Model.register("headless_bidaf")
class HeadlessBiDAF(Model):
    """
    This class implements Minjoon Seo's `Bidirectional Attention Flow model
    <https://www.semanticscholar.org/paper/Bidirectional-Attention-Flow-for-Machine-Seo-Kembhavi/7586b7cca1deba124af80609327395e613a20e9d>`_
    for answering reading comprehension questions (ICLR 2017).

    The basic layout is pretty simple: encode words as a combination of word embeddings and a
    character-level encoder, pass the word representations through a bi-LSTM/GRU, use a matrix of
    attentions to put question information into the passage word representations (this is the only
    part that is at all non-standard), pass this through another few layers of bi-LSTMs/GRUs.

    Parameters
    ----------
    vocab : ``Vocabulary``
    text_field_embedder : ``TextFieldEmbedder``
        Used to embed the ``question`` and ``passage`` ``TextFields`` we get as input to the model.
    num_highway_layers : ``int``
        The number of highway layers to use in between embedding the input and passing it through
        the phrase layer.
    phrase_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between embedding tokens
        and doing the bidirectional attention.
    attention_similarity_function : ``SimilarityFunction``
        The similarity function that we will use when comparing encoded passage and question
        representations.
    modeling_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in after the bidirectional
        attention.
    dropout : ``float``, optional (default=0.2)
        If greater than 0, we will apply dropout with this probability after all encoders (pytorch
        LSTMs do not apply dropout to their last layer).
    mask_lstms : ``bool``, optional (default=True)
        If ``False``, we will skip passing the mask to the LSTM layers.  This gives a ~2x speedup,
        with only a slight performance decrease, if any.  We haven't experimented much with this
        yet, but have confirmed that we still get very similar performance with much faster
        training times.  We still use the mask for all softmaxes, but avoid the shuffling that's
        required when using masking with pytorch LSTMs.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 attention_similarity_function: SimilarityFunction,
                 modeling_layer: Seq2SeqEncoder,
                 dropout: float = 0.2,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(HeadlessBiDAF, self).__init__(vocab)#, regularizer)

        self._text_field_embedder = text_field_embedder
        self._highway_layer = TimeDistributed(Highway(text_field_embedder.get_output_dim(), num_highway_layers))
        self._phrase_layer = phrase_layer
        self._matrix_attention = MatrixAttention(attention_similarity_function)
        self._modeling_layer = modeling_layer

        encoding_dim = phrase_layer.get_output_dim()
        modeling_dim = modeling_layer.get_output_dim()
        self.output_dim = modeling_dim

        # Bidaf has lots of layer dimensions which need to match up - these
        # aren't necessarily obvious from the configuration files, so we check
        # here.
        if modeling_layer.get_input_dim() != 4 * encoding_dim:
            raise ConfigurationError("The input dimension to the modeling_layer must be "
                                     "equal to 4 times the encoding dimension of the phrase_layer. "
                                     "Found {} and 4 * {} respectively.".format(modeling_layer.get_input_dim(),
                                                                                encoding_dim))
        if text_field_embedder.get_output_dim() != phrase_layer.get_input_dim():
            raise ConfigurationError("The output dimension of the "
                                     "text_field_embedder (embedding_dim + "
                                     "char_cnn) must match the input "
                                     "dimension of the phrase_encoder. "
                                     "Found {} and {}, respectively.".format(text_field_embedder.get_output_dim(),
                                                                             phrase_layer.get_input_dim()))
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)

    def forward(self, question, passage):
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passage : Dict[str, torch.LongTensor]
            From a ``TextField``.  The model assumes that this passage contains the answer to the
            question, and predicts the beginning and ending positions of the answer within the
            passage.

        Returns
        -------
        pair_rep : torch.FloatTensor?
            Tensor representing the final output of the BiDAF model
            to be plugged into the next module

        """
        embedded_question = self._highway_layer(self._text_field_embedder(question))
        embedded_passage = self._highway_layer(self._text_field_embedder(passage))
        batch_size = embedded_question.size(0)
        passage_length = embedded_passage.size(1)
        question_mask = util.get_text_field_mask(question).float()
        passage_mask = util.get_text_field_mask(passage).float()
        question_lstm_mask = question_mask if self._mask_lstms else None
        passage_lstm_mask = passage_mask if self._mask_lstms else None

        encoded_question = self._dropout(self._phrase_layer(embedded_question, question_lstm_mask))
        encoded_passage = self._dropout(self._phrase_layer(embedded_passage, passage_lstm_mask))
        encoding_dim = encoded_question.size(-1)

        # Attn over passage words for each question word
        # Shape: (batch_size, passage_length, question_length)
        passage_question_similarity = self._matrix_attention(encoded_passage, encoded_question)
        # Shape: (batch_size, passage_length, question_length)
        passage_question_attention = util.last_dim_softmax(passage_question_similarity, question_mask)
        # Shape: (batch_size, passage_length, encoding_dim)
        passage_question_vectors = util.weighted_sum(encoded_question, passage_question_attention)

        # We replace masked values with something really negative here, so they don't affect the
        # max below.
        masked_similarity = util.replace_masked_values(passage_question_similarity,
                                                       question_mask.unsqueeze(1),
                                                       -1e7)

        # Should be attn over question words for each passage word?
        # Shape: (batch_size, passage_length)
        question_passage_similarity = masked_similarity.max(dim=-1)[0].squeeze(-1)
        # Shape: (batch_size, passage_length)
        question_passage_attention = util.masked_softmax(question_passage_similarity, passage_mask)
        # Shape: (batch_size, encoding_dim)
        question_passage_vector = util.weighted_sum(encoded_passage, question_passage_attention)
        # Shape: (batch_size, passage_length, encoding_dim)
        tiled_question_passage_vector = question_passage_vector.unsqueeze(1).expand(batch_size, passage_length, encoding_dim)

        # Shape: (batch_size, passage_length, encoding_dim * 4)
        final_merged_passage = torch.cat([encoded_passage,
                                          passage_question_vectors,
                                          encoded_passage * passage_question_vectors,
                                          encoded_passage * tiled_question_passage_vector],
                                         dim=-1)

        modeled_passage = self._dropout(self._modeling_layer(final_merged_passage, passage_lstm_mask))
        modeling_dim = modeled_passage.size(-1)

        pair_rep = self._dropout(torch.cat([final_merged_passage, modeled_passage], dim=-1))
        return pair_rep

    @classmethod
    def from_params(cls, vocab: Vocabulary, params: Params) -> 'BidirectionalAttentionFlow':
        embedder_params = params.pop("text_field_embedder")
        text_field_embedder = TextFieldEmbedder.from_params(vocab, embedder_params)
        num_highway_layers = params.pop("num_highway_layers")
        phrase_layer = Seq2SeqEncoder.from_params(params.pop("phrase_layer"))
        similarity_function = SimilarityFunction.from_params(params.pop("similarity_function"))
        modeling_layer = Seq2SeqEncoder.from_params(params.pop("modeling_layer"))
        dropout = params.pop('dropout', 0.2)

        initializer = InitializerApplicator.from_params(params.pop('initializer', []))
        regularizer = RegularizerApplicator.from_params(params.pop('regularizer', []))

        mask_lstms = params.pop('mask_lstms', True)
        params.assert_empty(cls.__name__)
        return cls(vocab=vocab,
                   text_field_embedder=text_field_embedder,
                   num_highway_layers=num_highway_layers,
                   phrase_layer=phrase_layer,
                   attention_similarity_function=similarity_function,
                   modeling_layer=modeling_layer,
                   dropout=dropout,
                   mask_lstms=mask_lstms,
                   initializer=initializer,
                   regularizer=regularizer)
