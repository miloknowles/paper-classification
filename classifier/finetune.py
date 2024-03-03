import sys; sys.path.append("..")
import logging

import torch

from transformers import AutoModelForSequenceClassification, AutoTokenizer
from transformers import TrainingArguments, Trainer
from datasets import load_dataset

from classifier.paths import data_folder, models_folder
from classifier.train_utils import get_best_system_device

import numpy as np
import evaluate


logger = logging.getLogger(__name__)

metrics = dict(
  accuracy=evaluate.load("accuracy"),
  f1=evaluate.load("f1"),
  precision=evaluate.load("precision"),
  recall=evaluate.load("recall"),
)


def compute_metrics(pred_eval: tuple[torch.Tensor, torch.Tensor]) -> dict[str, float]:
  """Takes a tuple of logits and labels and returns a dictionary of metrics."""
  logits, labels = pred_eval
  predictions = np.argmax(logits, axis=-1)
  return {name: metric.compute(predictions=predictions, references=labels) for name, metric in metrics.items()}


def get_max_steps(train_path: str, num_train_epochs: int, batch_size: int) -> int:
  """Get the maximum number of training steps.
  
  This is required for the `TrainingArguments` object, since we're using an
  iterable dataset that is backed by a generator with unknown length.
  """
  with open(train_path, 'r') as f:
    for n_examples, _ in enumerate(f):
      pass
  return (n_examples + 1) * num_train_epochs // batch_size


class TrainerWithCustomLoss(Trainer):
  """Subclasses `Trainer` to use a custom loss function.
  
  This allows us to use a weighted cross entropy loss to deal with class imbalance.
  """
  def __init__(
    self,
    compute_loss_fn: callable,
    *args,
    **kwargs
  ):
    super().__init__(*args, **kwargs)
    self.compute_loss_fn = compute_loss_fn

  def compute_loss(self, model, inputs, return_outputs=False):
    """Compute using the custom loss function."""
    labels = inputs.pop("labels")
    outputs = model(**inputs)
    logits = outputs.logits
    loss = self.compute_loss_fn(logits, labels)
    return (loss, outputs) if return_outputs else loss


def main():
  num_labels = 2
  num_train_epochs = 50
  batch_size = 16

  model_name = "distilbert/distilbert-base-uncased"
  # model_name = "bert-base-uncased"
  run_name = "debugging"
  device = get_best_system_device()

  id2label = {0: "False", 1: "True"}
  label2id = {"False": 0, "True": 1}
  label_loss_weights = torch.Tensor([1.0, 10.0]).to(device)

  model = AutoModelForSequenceClassification.from_pretrained(
    model_name,
    num_labels=num_labels,
    id2label=id2label,
    label2id=label2id,
  )
  # https://stackoverflow.com/questions/69842980/asking-to-truncate-to-max-length-but-no-maximum-length-is-provided-and-the-model
  tokenizer = AutoTokenizer.from_pretrained(model_name, model_max_length=512)

  # This dataset has columns: `text` and `label`.
  dataset = load_dataset("json", data_files={
    "train": str(data_folder / "finetuning" / "augmented_train.jsonl"),
    "val": str(data_folder / "finetuning" / "val.jsonl"),
  }).select_columns(["text", "label"])

  def convert_labels(examples: dict[str, list[int | str]]):
    """Convert the `label` field to a numeric value (it's "True" or "False" in the raw data)."""
    return {"label": [{"True": 1, "False": 0}[label] for label in examples["label"]]}

  def tokenize(examples: dict[str, list[int | str]]):
    """Tokenize the `text` field of all examples."""
    return tokenizer(examples["text"], truncation=True, padding="max_length")

  dataset = dataset.map(convert_labels, batched=True)
  dataset = dataset.map(tokenize, batched=True).shuffle(seed=42)

  training_args = TrainingArguments(
    output_dir=models_folder / run_name,
    evaluation_strategy="epoch",
    num_train_epochs=num_train_epochs,
    per_device_train_batch_size=batch_size,
    per_device_eval_batch_size=batch_size,
    save_strategy="epoch",
    # learning_rate=1e-4,
    # max_steps=get_max_steps(data_folder / "finetuning" / "train.jsonl", num_train_epochs, batch_size),
  )

  # trainer = TrainerWithCustomLoss(
  #   compute_loss_fn=torch.nn.CrossEntropyLoss(weight=label_loss_weights, reduction="mean"),
  #   model=model,
  #   args=training_args,
  #   train_dataset=dataset["train"],
  #   eval_dataset=dataset["val"],
  #   compute_metrics=compute_metrics,
  #   tokenizer=tokenizer,
  # )

  trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=dataset["train"],
    eval_dataset=dataset["val"],
    compute_metrics=compute_metrics,
    tokenizer=tokenizer,
  )

  trainer.train()


if __name__ == "__main__":
  main()