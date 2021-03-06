import logging
import torch
import torch.autograd as autograd
import torch.nn as nn
import numpy as np

import model.utils as utils


class StackRNN(object):
    def __init__(self, cell, initial_state, dropout, get_output, p_empty_embedding=None):
        self.cell = cell
        self.dropout = dropout
        self.s = [(initial_state, None)]
        self.empty = None
        self.get_output = get_output
        if p_empty_embedding is not None:
            self.empty = p_empty_embedding

    def push(self, expr, extra=None):
        self.dropout(self.s[-1][0][0])
        self.s.append((self.cell(expr, self.s[-1][0]), extra))

    def pop(self):
        return self.s.pop()[1]

    def embedding(self):
        return self.get_output(self.s[-1][0]) if len(self.s) > 1 else self.empty

    def back_to_init(self):
        while self.__len__() > 0:
            self.pop()

    def clear(self):
        self.s.reverse()
        self.back_to_init()

    def __len__(self):
        return len(self.s) - 1

class StackRNN_2Layer(object):
    def __init__(self, cell, initial_state, dropout, get_output, p_empty_embedding=None):
        self.cell1 = cell
        self.dropout = dropout
        self.cell2 = nn.LSTMCell(self.cell1.hidden_size, self.cell1.hidden_size)
        self.layer1 = [(initial_state, None)]
        self.layer2 = [(initial_state, None)]
        self.empty = None
        self.get_output = get_output
        if p_empty_embedding is not None:
            self.empty = p_empty_embedding

    def push(self, expr, extra=None):
        self.dropout(self.layer1[-1][0][0])
        self.layer1.append((self.cell1(expr, self.layer1[-1][0]), extra))
        mid_h = self.get_output(self.layer1[-1][0])
        self.dropout(self.layer2[-1][0][0])
        self.layer2.append((self.cell2(mid_h, self.layer2[-1][0]), extra))

    def pop(self):
        layer1 = self.layer1.pop()[1]
        return self.layer2.pop()[1]

    def back_to_init(self):
        while self.__len__() > 0:
            self.pop()

    def clear(self):
        self.layer1.reverse()
        self.layer2.reverse()
        self.back_to_init()

    def embedding(self):
        return self.get_output(self.layer2[-1][0]) if len(self.layer2) > 1 else self.empty

    def __len__(self):
        assert len(self.layer1) == len(self.layer2)
        return len(self.layer1) - 1


class TransitionNER(nn.Module):

    def __init__(self, mode, action2idx, word2idx, label2idx, char2idx, ner_map, vocab_size, action_size, embedding_dim, action_embedding_dim, char_embedding_dim,
                 hidden_dim, char_hidden_dim, rnn_layers, dropout_ratio, use_spelling, char_structure, is_cuda):
        super(TransitionNER, self).__init__()
        self.embedding_dim = embedding_dim
        self.mode = mode
        self.hidden_dim = hidden_dim
        self.vocab_size = vocab_size
        self.action2idx = action2idx
        self.label2idx = label2idx
        self.char2idx = char2idx
        self.use_spelling = use_spelling
        self.char_structure = char_structure
        if is_cuda >=0:
            self.gpu_triger = True
        else:
            self.gpu_triger = False
        self.idx2label = {v: k for k, v in label2idx.items()}
        self.idx2action = {v: k for k, v in action2idx.items()}
        self.idx2word = {v: k for k, v in word2idx.items()}
        self.idx2char = {v: k for k, v in char2idx.items()}
        self.ner_map = ner_map

        self.word_embeds = nn.Embedding(vocab_size, embedding_dim)
        self.action_embeds = nn.Embedding(action_size, action_embedding_dim)
        self.relation_embeds = nn.Embedding(action_size, action_embedding_dim)

        if self.use_spelling:
            self.char_embeds = nn.Embedding(len(self.char2idx), char_embedding_dim)
            if self.char_structure == 'lstm':
                self.tok_embedding_dim = self.embedding_dim + char_hidden_dim*2
                self.unk_char_embeds = nn.Parameter(torch.randn(1, char_hidden_dim * 2), requires_grad=True)
                self.pad_char_embeds = nn.Parameter(torch.zeros(1, char_hidden_dim * 2))
                self.char_bi_lstm = nn.LSTM(char_embedding_dim, char_hidden_dim, num_layers=rnn_layers, bidirectional=True, dropout=dropout_ratio)
            elif self.char_structure == 'cnn':
                self.tok_embedding_dim = self.embedding_dim + char_hidden_dim
                self.pad_char_embeds = nn.Parameter(torch.zeros(1, char_hidden_dim ))
                self.unk_char_embeds = nn.Parameter(torch.randn(1, char_hidden_dim), requires_grad=True)
                self.conv1d = nn.Conv1d(char_embedding_dim, char_hidden_dim, 3, padding=2)
        else:
            self.tok_embedding_dim = self.embedding_dim

        self.buffer_lstm = nn.LSTMCell(self.tok_embedding_dim, hidden_dim)
        self.stack_lstm = nn.LSTMCell(self.tok_embedding_dim, hidden_dim)
        self.action_lstm = nn.LSTMCell(action_embedding_dim, hidden_dim)
        self.output_lstm = nn.LSTMCell(self.tok_embedding_dim, hidden_dim)
        self.entity_forward_lstm = nn.LSTMCell(self.tok_embedding_dim, hidden_dim)
        self.entity_backward_lstm = nn.LSTMCell(self.tok_embedding_dim, hidden_dim)

        self.ac_lstm = nn.LSTM(action_embedding_dim, hidden_dim, num_layers=rnn_layers, bidirectional=False, dropout=dropout_ratio)
        self.lstm = nn.LSTM(self.tok_embedding_dim, hidden_dim, num_layers=rnn_layers, bidirectional=False,
                                    dropout=dropout_ratio)
        self.rnn_layers = rnn_layers

        self.dropout_e = nn.Dropout(p=dropout_ratio)
        self.dropout = nn.Dropout(p=dropout_ratio)

        self.init_buffer = utils.xavier_init(self.gpu_triger,1,hidden_dim)
        self.empty_emb = nn.Parameter(torch.randn(1, hidden_dim))
        self.lstms_output_2_softmax = nn.Linear(hidden_dim * 4, hidden_dim)
        self.output_2_act = nn.Linear(hidden_dim, len(ner_map)+2)
        self.entity_2_output = nn.Linear(hidden_dim*2 + action_embedding_dim, self.tok_embedding_dim)

        self.batch_size = 1
        self.seq_length = 1



    def _rnn_get_output(self, state):
        return state[0]

    def get_possible_actions(self, stack, buffer):
        valid_actions = []
        if len(buffer) > 0:
            valid_actions.append(self.action2idx["SHIFT"])
        if len(stack) > 0:
            valid_actions += [self.action2idx[ner_action] for ner_action in self.ner_map.keys()]
        else:
            valid_actions.append(self.action2idx["OUT"])
        return valid_actions

    def rand_init_hidden(self):

        if self.gpu_triger is True:
            return autograd.Variable(
                torch.randn(2 * self.rnn_layers, self.batch_size, self.hidden_dim // 2)).cuda(), autograd.Variable(
                torch.randn(2 * self.rnn_layers, self.batch_size, self.hidden_dim // 2)).cuda()
        else:
            return autograd.Variable(
                torch.randn(2 * self.rnn_layers, self.batch_size, self.hidden_dim // 2)), autograd.Variable(
                torch.randn(2 * self.rnn_layers, self.batch_size, self.hidden_dim // 2))

    def set_seq_size(self, sentence):

        tmp = sentence.size()
        self.seq_length = tmp[0]
        self.batch_size = 1

    def set_batch_seq_size(self, sentence):

        tmp = sentence.size()
        self.seq_length = tmp[1]
        self.batch_size = tmp[0]

    def load_pretrained_embedding(self, pre_embeddings):

        assert (pre_embeddings.size()[1] == self.embedding_dim)
        self.word_embeds.weight = nn.Parameter(pre_embeddings)


    def rand_init(self, init_word_embedding=False, init_action_embedding=True, init_relation_embedding=True):

        if init_word_embedding:
            utils.init_embedding(self.word_embeds.weight)
        if init_action_embedding:
            utils.init_embedding(self.action_embeds.weight)
        if init_relation_embedding:
            utils.init_embedding(self.relation_embeds.weight)

        if self.use_spelling:
            utils.init_embedding(self.char_embeds.weight)
        if self.use_spelling and self.char_structure == 'lstm':
            utils.init_lstm(self.char_bi_lstm)
            
        utils.init_linear(self.lstms_output_2_softmax)
        utils.init_linear(self.output_2_act)
        utils.init_linear(self.entity_2_output)
        
        utils.init_lstm(self.lstm)
        utils.init_lstm_cell(self.buffer_lstm)
        utils.init_lstm_cell(self.action_lstm)
        utils.init_lstm_cell(self.stack_lstm)
        utils.init_lstm_cell(self.output_lstm)
        utils.init_lstm_cell(self.entity_forward_lstm)
        utils.init_lstm_cell(self.entity_backward_lstm)

    def forward(self, sentence, actions=None, hidden=None):

        sentence = sentence.squeeze(0)
        self.set_seq_size(sentence)
        word_embeds = self.dropout_e(self.word_embeds(sentence))
        if self.mode == 'train':
            actions = actions.squeeze(0)
            action_embeds = self.dropout_e(self.action_embeds(actions))
            relation_embeds = self.dropout_e(self.relation_embeds(actions))
        action_count = 0

        lstm_initial = (utils.xavier_init(self.gpu_triger, 1, self.hidden_dim), utils.xavier_init(self.gpu_triger, 1, self.hidden_dim))

        buffer = StackRNN(self.buffer_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)
        stack = StackRNN(self.stack_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)
        action = StackRNN(self.action_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)
        output = StackRNN(self.output_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)
        ent_f = StackRNN(self.entity_forward_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)
        ent_b = StackRNN(self.entity_backward_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb)

        pre_actions = []
        losses = []
        right = 0

        sentence_array = sentence.data.cpu().numpy()
        token_embedding = list()

        for word_idx in range(len(sentence_array)):
            if self.use_spelling:
                if sentence_array[word_idx] == 0:
                    tok_rep = torch.cat([word_embeds[word_idx].unsqueeze(0), self.unk_char_embeds], 1)
                else:
                    word = sentence_array[word_idx]
                    chars_in_word = [self.char2idx[char] for char in self.idx2word[word]]
                    chars_Tensor = utils.varible(torch.from_numpy(np.array(chars_in_word)), self.gpu_triger)
                    chars_embeds = self.dropout_e(self.char_embeds(chars_Tensor))
                    if self.char_structure == 'lstm':
                        char_o, hidden = self.char_bi_lstm(chars_embeds.unsqueeze(1), hidden)
                        char_out = torch.chunk(hidden[0].squeeze(1), 2, 0)
                        tok_rep = torch.cat([word_embeds[word_idx].unsqueeze(0), char_out[0], char_out[1]], 1)
                    elif self.char_structure == 'cnn':
                        char = chars_embeds.unsqueeze(0)
                        char = char.transpose(1, 2)
                        char, _ = self.conv1d(char).max(dim=2)
                        char = torch.tanh(char)
                        tok_rep = torch.cat([word_embeds[word_idx].unsqueeze(0), char], 1)
            else:
                tok_rep = word_embeds[word_idx].unsqueeze(0)
            if word_idx == 0:
                token_embedding = tok_rep
            else:
                token_embedding = torch.cat([token_embedding, tok_rep], 0)

        for i in range(token_embedding.size()[0]):
            tok_embed = token_embedding[token_embedding.size()[0]-1-i].unsqueeze(0)
            tok = sentence.data[token_embedding.size()[0]-1-i]
            buffer.push(tok_embed, (tok_embed, self.idx2word[tok]))

        while len(buffer) > 0 or len(stack) > 0:
            valid_actions = self.get_possible_actions(stack, buffer)
            log_probs = None
            if len(valid_actions)>1:

                lstms_output = torch.cat([buffer.embedding(), stack.embedding(), output.embedding(), action.embedding()], 1)
                hidden_output = torch.tanh(self.lstms_output_2_softmax(self.dropout(lstms_output)))
                if self.gpu_triger is True:
                    logits = self.output_2_act(hidden_output)[0][torch.autograd.Variable(torch.LongTensor(valid_actions)).cuda()]
                else:
                    logits = self.output_2_act(hidden_output)[0][torch.autograd.Variable(torch.LongTensor(valid_actions))]
                valid_action_tbl = {a: i for i, a in enumerate(valid_actions)}
                log_probs = torch.nn.functional.log_softmax(logits)
                action_idx = torch.max(log_probs.cpu(), 0)[1][0].data.numpy()[0]
                action_predict = valid_actions[action_idx]
                pre_actions.append(action_predict)
                if self.mode == 'train':
                    if log_probs is not None:
                        losses.append(log_probs[valid_action_tbl[actions.data[action_count]]])

            if self.mode == 'train':
                real_action = self.idx2action[actions.data[action_count]]
                act_embedding = action_embeds[action_count].unsqueeze(0)
                rel_embedding = relation_embeds[action_count].unsqueeze(0)
            elif self.mode == 'predict':
                real_action = self.idx2action[action_predict]
                action_predict_tensor = utils.varible(torch.from_numpy(np.array([action_predict])), self.gpu_triger)
                action_embeds = self.dropout_e(self.action_embeds(action_predict_tensor))
                relation_embeds = self.dropout_e(self.relation_embeds(action_predict_tensor))
                act_embedding = action_embeds[0].unsqueeze(0)
                rel_embedding = relation_embeds[0].unsqueeze(0)

            if real_action == self.idx2action[action_predict]:
                right += 1
            action.push(act_embedding,(act_embedding, real_action))
            if real_action.startswith('S'):
                assert len(buffer) > 0
                tok_buffer_embedding, buffer_token = buffer.pop()
                stack.push(tok_buffer_embedding, (tok_buffer_embedding, buffer_token))
            elif real_action.startswith('O'):
                assert len(buffer) > 0
                tok_buffer_embedding, buffer_token = buffer.pop()
                output.push(tok_buffer_embedding, (tok_buffer_embedding, buffer_token))
            elif real_action.startswith('R'):
                ent =''
                entity = []
                assert len(stack) > 0
                while len(stack) > 0:
                    tok_stack_embedding, stack_token = stack.pop()
                    entity.append([tok_stack_embedding, stack_token])
                if len(entity) > 1:

                    for i in range(len(entity)):
                        ent_f.push(entity[i][0], (entity[i][0],entity[i][1]))
                        ent_b.push(entity[len(entity)-i-1][0], (entity[len(entity)-i-1][0], entity[len(entity)-i-1][1]))
                        ent += entity[i][1]
                        ent += ' '
                    entity_input = self.dropout(torch.cat([ent_f.embedding(), ent_b.embedding()], 1))
                else:
                    ent_f.push(entity[0][0], (entity[0][0], entity[0][1]))
                    ent_b.push(entity[0][0], (entity[0][0], entity[0][1]))
                    ent = entity[0][1]
                    entity_input = self.dropout(torch.cat([ent_f.embedding(), ent_b.embedding()], 1))
                ent_f.clear()
                ent_b.clear()
                output_input = self.entity_2_output(torch.cat([entity_input, rel_embedding], 1))
                output.push(output_input, (entity_input, ent))
            action_count += 1

        if len(losses) > 0:
            loss = -torch.sum(torch.cat(losses))
        else:
            loss = -1

        return loss, pre_actions, right if len(losses) > 0 else None

    def forward_batch(self, sentences, actions=None, hidden=None):

        self.set_batch_seq_size(sentences) #sentences [batch_size, max_len]
        word_embeds = self.dropout_e(self.word_embeds(sentences)) #[batch_size, max_len, embeddind_size]
        if self.mode == 'train':
            action_embeds = self.dropout_e(self.action_embeds(actions))
            relation_embeds = self.dropout_e(self.relation_embeds(actions))
            action_output, _ = self.ac_lstm(action_embeds.transpose(0, 1))
            action_output = action_output.transpose(0, 1)

        lstm_initial = (utils.xavier_init(self.gpu_triger, 1, self.hidden_dim), utils.xavier_init(self.gpu_triger, 1, self.hidden_dim))
        buffer = [[] for i in range(self.batch_size)]
        losses = [[] for i in range(self.batch_size)]
        right = [0 for i in range(self.batch_size)]
        predict_actions = [[] for i in range(self.batch_size)]
        stack = [StackRNN(self.stack_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb) for i in range(self.batch_size)]
        output = [StackRNN(self.output_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb) for i in range(self.batch_size)]
        ent_f = [StackRNN(self.entity_forward_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb) for i in range(self.batch_size)]
        ent_b = [StackRNN(self.entity_backward_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb) for i in range(self.batch_size)]
        if self.mode == 'predict':
            action = [StackRNN(self.action_lstm, lstm_initial, self.dropout, self._rnn_get_output, self.empty_emb) for i in range(self.batch_size)]
        sentence_array = sentences.data.cpu().numpy()
        sents_len = []
        token_embedds = None
        for sent_idx in range(len(sentence_array)):
            count_words = 0
            token_embedding = None
            for word_idx in reversed(range(len(sentence_array[sent_idx]))):
                if self.use_spelling:
                    if sentence_array[sent_idx][word_idx] == 1 :
                        tok_rep = torch.cat([word_embeds[sent_idx][word_idx].unsqueeze(0), self.pad_char_embeds], 1)
                    elif sentence_array[sent_idx][word_idx] == 0 :
                        count_words += 1
                        tok_rep = torch.cat([word_embeds[sent_idx][word_idx].unsqueeze(0), self.unk_char_embeds], 1)
                    else:
                        count_words += 1
                        word = sentence_array[sent_idx][word_idx]
                        chars_in_word = [self.char2idx[char] for char in self.idx2word[word]]
                        chars_Tensor = utils.varible(torch.from_numpy(np.array(chars_in_word)), self.gpu_triger)
                        chars_embeds = self.dropout_e(self.char_embeds(chars_Tensor))
                        if self.char_structure == 'lstm':
                            char_o, hidden = self.char_bi_lstm(chars_embeds.unsqueeze(1), hidden)
                            char_out = torch.chunk(hidden[0].squeeze(1), 2, 0)
                            tok_rep = torch.cat([word_embeds[sent_idx][word_idx].unsqueeze(0), char_out[0], char_out[1]], 1)
                        elif self.char_structure == 'cnn':
                            char = chars_embeds.unsqueeze(0)
                            char = char.transpose(1, 2)
                            char, _ = self.conv1d(char).max(dim=2)
                            char = torch.tanh(char)
                            tok_rep = torch.cat([word_embeds[sent_idx][word_idx].unsqueeze(0), char], 1)
                else:
                    if sentence_array[sent_idx][word_idx] != 1:
                        count_words += 1
                    tok_rep = word_embeds[sent_idx][word_idx].unsqueeze(0)
                if token_embedding is None:
                    token_embedding = tok_rep
                else:
                    token_embedding = torch.cat([token_embedding, tok_rep], 0)

            sents_len.append(count_words)
            if token_embedds is None:
                token_embedds = token_embedding.unsqueeze(0)
            else:
                token_embedds = torch.cat([token_embedds, token_embedding.unsqueeze(0)], 0)

        tokens = token_embedds.transpose(0, 1)
        tok_output, hidden = self.lstm(tokens)  #[max_len, batch_size, hidden_dim]
        tok_output = tok_output.transpose(0, 1)

        for idx in range(tok_output.size(0)):
            emd_idx =sents_len[idx]-1
            buffer[idx].append([self.init_buffer])
            for word_idx in range(tok_output.size(1)-sents_len[idx], tok_output.size(1)):
                buffer[idx].append([tok_output[idx][word_idx].unsqueeze(0), token_embedds[idx][word_idx].unsqueeze(0), self.idx2word[sentence_array[idx][emd_idx]]])
                emd_idx -= 1

        for batch_idx in range(self.batch_size):

            action_count = 0
            while len(buffer[batch_idx]) > 1 or len(stack[batch_idx]) > 0:
                valid_actions = self.get_possible_actions(stack[batch_idx], buffer[batch_idx])
                log_probs = None
                if len(valid_actions) > 1:
                    if self.mode == 'train':
                        if action_count == 0:
                            lstms_output = torch.cat(
                                [buffer[batch_idx][-1][0], stack[batch_idx].embedding(), output[batch_idx].embedding(),
                                 lstm_initial[0]], 1)
                        else:
                            lstms_output = torch.cat(
                                [buffer[batch_idx][-1][0], stack[batch_idx].embedding(), output[batch_idx].embedding(),
                                 action_output[batch_idx][action_count-1].unsqueeze(0)], 1)
                    elif self.mode == 'predict':
                        lstms_output = torch.cat(
                            [buffer[batch_idx][-1][0], stack[batch_idx].embedding(), output[batch_idx].embedding(),
                             action[batch_idx].embedding(),], 1)

                    hidden_output = torch.tanh(self.lstms_output_2_softmax(self.dropout(lstms_output)))
                    if self.gpu_triger is True:
                        logits = self.output_2_act(hidden_output)[0][
                            torch.autograd.Variable(torch.LongTensor(valid_actions)).cuda()]
                    else:
                        logits = self.output_2_act(hidden_output)[0][
                            torch.autograd.Variable(torch.LongTensor(valid_actions))]
                    valid_action_tbl = {a: i for i, a in enumerate(valid_actions)}
                    log_probs = torch.nn.functional.log_softmax(logits)
                    action_idx = torch.max(log_probs.cpu(), 0)[1][0].data.numpy()[0]
                    action_predict = valid_actions[action_idx]
                    predict_actions[batch_idx].append(action_predict)
                    if self.mode == 'train':
                        if log_probs is not None:
                            losses[batch_idx].append(log_probs[valid_action_tbl[actions.data[batch_idx][action_count]]])

                if self.mode == 'train':
                    real_action = self.idx2action[actions.data[batch_idx][action_count]]
                    rel_embedding = relation_embeds[batch_idx][action_count].unsqueeze(0)
                elif self.mode == 'predict':
                    real_action = self.idx2action[action_predict]
                    action_predict_tensor = utils.varible(torch.from_numpy(np.array([action_predict])), self.gpu_triger)
                    action_embeds = self.dropout_e(self.action_embeds(action_predict_tensor))
                    relation_embeds = self.dropout_e(self.relation_embeds(action_predict_tensor))
                    act_embedding = action_embeds[0].unsqueeze(0)
                    rel_embedding = relation_embeds[0].unsqueeze(0)
                    action[batch_idx].push(act_embedding, (act_embedding, real_action))

                if real_action == self.idx2action[action_predict]:
                    right[batch_idx] += 1
                if real_action.startswith('S'):
                    assert len(buffer[batch_idx]) > 0
                    _, tok_buffer_embedding, buffer_token = buffer[batch_idx].pop()
                    stack[batch_idx].push(tok_buffer_embedding, (tok_buffer_embedding, buffer_token))
                elif real_action.startswith('O'):
                    assert len(buffer[batch_idx]) > 0
                    _, tok_buffer_embedding, buffer_token = buffer[batch_idx].pop()
                    output[batch_idx].push(tok_buffer_embedding, (tok_buffer_embedding, buffer_token))
                elif real_action.startswith('R'):
                    ent = ''
                    entity = []
                    assert len(stack[batch_idx]) > 0
                    while len(stack[batch_idx]) > 0:
                        tok_stack_embedding, stack_token = stack[batch_idx].pop()
                        entity.append([tok_stack_embedding, stack_token])
                    if len(entity) > 1:

                        for i in range(len(entity)):
                            ent_f[batch_idx].push(entity[i][0], (entity[i][0], entity[i][1]))
                            ent_b[batch_idx].push(entity[len(entity) - i - 1][0],
                                       (entity[len(entity) - i - 1][0], entity[len(entity) - i - 1][1]))
                            ent += entity[i][1]
                            ent += ' '
                        entity_input = self.dropout(torch.cat([ent_b[batch_idx].embedding(), ent_f[batch_idx].embedding()], 1))
                    else:
                        ent_f[batch_idx].push(entity[0][0], (entity[0][0], entity[0][1]))
                        ent_b[batch_idx].push(entity[0][0], (entity[0][0], entity[0][1]))
                        ent = entity[0][1]
                        entity_input = self.dropout(torch.cat([ent_f[batch_idx].embedding(), ent_b[batch_idx].embedding()], 1))
                    ent_f[batch_idx].clear()
                    ent_b[batch_idx].clear()
                    output_input = self.entity_2_output(torch.cat([entity_input, rel_embedding], 1))
                    output[batch_idx].push(output_input, (entity_input, ent))
                action_count += 1

        loss = 0
        for idx in range(self.batch_size):
            loss += -torch.sum(torch.cat(losses[idx]))

        return loss, predict_actions, right if len(losses) > 0 else None
