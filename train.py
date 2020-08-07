import argparse
import logging

import numpy as np
import pandas as pd
from torch import where, ones_like, LongTensor, no_grad, argmax
from torch.nn import CrossEntropyLoss
from gluonnlp.data import SentencepieceTokenizer, PadSequence
from kogpt2.pytorch_kogpt2 import get_pytorch_kogpt2_model
from kogpt2.utils import get_tokenizer
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.core.lightning import LightningModule
from torch.utils.data import DataLoader, Dataset
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup

parser = argparse.ArgumentParser(description='Ruby based on KoGPT-2')

parser.add_argument('--chat', action='store_true', default=False)
parser.add_argument('--model', type=str, default='model_chp/model_last.ckpt')
parser.add_argument('--trian', action='store_true', default=False, help='training the model')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

U_TKN = '<usr>'
S_TKN = '<sys>'
BOS = '<s>'
EOS = '</s>'
MASK = '<unused0>'
SENT = '<unused1>'

class CharecterDataset(Dataset):
  def __init__(self, data, tok_path, vocab, max_len=64):
    self._data = data
    self._tok_path = tok_path
    self.tokenizer = None
    self.first = True
    self.q_token = U_TKN
    self.a_token = S_TKN
    self.sent_token = SENT
    self.bos = BOS
    self.eos = EOS
    self.maskt = MASK
    self.vocab = vocab
    self.max_len = max_len
    self.padder = PadSequence(max_len, pad_val=self.vocab[self.vocab.padding_token])

  def _activate_sp(self):
    self.tokenizer = SentencepieceTokenizer(self._tok_path, 0, 0)

  def data_len(self):
    return len(self._data)

  def __getitem__(self, index):
    if self.tokenizer is None:
      self._activate_sp()
    turn = self._data_iloc[index]
    q = turn['Q']
    a = turn['A']
    sentiment = '1'
    q_toked = [self.q_token] + self.tokenizer(q) + [self.eos] + [self.sent_token] + self.tokenizer(sentiment) + [self.eos]
    q_len = len(q_toked)
    a_toked = [self.a_token] + self.tokenizer(a) + [self.eos]
    a_len = len(a_toked)

    if q_len + a_len > self.max_len:
      remains = self.max_len - q_len
      a_len = remains
      a_toked = a_toked[-a_len:]
      assert a_len == len(a_toked)

    labels = [self.maskt] * q_len + a_toked[1:]

    if self.first:
      logging.info("contexts : {}".format(q))
      logging.info("toked ctx: {}".format(q_toked))
      logging.info("response : {}".format(a))
      logging.info("toked response : {}".format(a_toked))
      logging.info('labels {}'.format(labels))
      self.first = False
    
    mask = [0] * q_len + [1] * a_len + [0] * (self.max_len - q_len - a_len)
    return (self.padder(self.vocab[q_toked + a_toked]), np.array(mask), self.padder(self.vocab[labels]))

class KoGPT2Chat(LightningModule):
  def __init__(self, hparams, **kwargs):
    super(KoGPT2Chat, self).__init__()
    self.hparams = hparams
    self.tok_path = get_tokenizer()
    self.neg = -1e18
    self.kogpt2, self.vocab = get_pytorch_kogpt2_model()
    self.loss_func = CrossEntropyLoss(reduction='none')

  @staticmethod
  def add_model_specific_args(parent_parser):
    parser = argparse.ArgumentParser(parents=[parent_parser], add_help=False)
    parser.add_argument('--max-len', type=int, default=32, help='max sentence length on input (default: 32)')
    parser.add_argument('--batch-size', type=int, default=96, help='batch size for training (default: 96)')
    parser.add_argument('--lr', type=float, default=5e-5, help='The initial learning rate')
    parser.add_argument('--warmup_ratio', type=float, default=0.1, help='warmup ratio')
    return parser

  def forward(self, inputs):
    return self.kogpt2(inputs)[0]

  def training_step(self, batch, batch_index):
    token_ids, mask, label = batch
    out = self(token_ids)
    mask_3d = mask.unsqueeze(dim=2).repeat_interleave(repeats=out.shape[2], dim=2)
    mask_out = where(mask_3d==1, out, self.neg * ones_like(out))
    loss = self.loss_func(mask_out.transpose(2, 1), label)
    loss_avg = loss.sum() / mask.sum()
    tensorboard_logs = {'train_loss': loss_avg}
    return {'loss': loss_avg, 'log': tensorboard_logs}

  def configure_optimizers(self):
    param_optimizer = list(self.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
      {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
      {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=self.hparams.lr, correct_bias=False)

    num_train_steps = len(self.train_dataloader()) * self.hparams.max_epochs
    num_warmup_steps = int(num_train_steps * self.hparams.warmup_ratio)
    scheduler = get_cosine_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=num_train_steps)
    lr_scheduler = {'scheduler': scheduler, 'name': 'cosine_schedule_with_warmup', 'monitor': 'loss', 'interval': 'step', 'frequency': 1}
    return [optimizer], lr_scheduler

  def _collate_fn(self, batch):
    data = [item[0] for item in batch]
    mask = [item[1] for item in batch]
    label = [item[2] for item in batch]
    return LongTensor(data), LongTensor(mask), LongTensor(label)

  def trian_dataloader(self):
    data = pd.read_csv('Chatbot_data/ChatbotData.csv')
    self.train_set = CharecterDataset(data, self.tok_path, self.vocab, max_len=self.hparams.max_len)
    train_dataloader = DataLoader(self.train_set, batch_size=self.hparams.batch_size, num_workers=2, shuffle=True, collate_fn=self._collate_fn)
    return train_dataloader

  def chat(self, sent='0'):
    self.tok_path
    tok = SentencepieceTokenizer(self.tok_path, num_best=0, alpha=0.1)
    sent_tokens = tok(sent)
    with no_grad():
      while True:
        q = input('user > ').strip()
        if q == 'quit':
          break
        q_tok = tok(q)
        a = ''
        a_tok = []
        while True:
          input_ids = LongTensor([
            self.vocab[U_TKN]] + self.vocab[q_tok] +
            self.vocab[EOS, SENT] + self.vocab[sent_tokens] +
            self.vocab[EOS, S_TKN] + self.vocab[a_tok]).unsqueeze(dim=0)
          pred = self(input_ids)
          gen = self.vocab.to_tokens(argmax(pred, dim=-1).squeeze().numpy().tolist())[-1]
          if gen == EOS:
            break
        a += gen.replace('_', ' ')
        a_tok = tok(a)
      print('Ruda > {}'.format(a.strip()))

parser = KoGPT2Chat.add_model_specific_args(parser)
parser = Trainer.add_argparse_args(parser)
args = parser.parse_args()
logging.info(args)

if __name__ == 'main':
  if args.train:
    checkpoint_callback = ModelCheckpoint(
      filepath='model_chp/{epoch:02d}-{loss:.2f}',
      verbose=True,
      save_last=True,
      monitor='loss',
      mode='min',
      prefix='model_'
    )
    
    model = KoGPT2Chat(args)
    model.train()
    trainer = Trainer.from_argparse_args(args, checkpoint_callback=checkpoint_callback, gradient_clip_val=1.0)
    trainer.fit(model)
    logging.info('best model path {}'.format(checkpoint_callback.best_model_path))
    if args.chat:
      model = KoGPT2Chat.load_from_checkpoint(args.model_params)
      model.chat()
