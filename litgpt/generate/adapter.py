# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.
# [2024] CHANGES MADE BY Yichen Gong, Delong Ran. Licensed under the Apache License 2.0, see LICENSE file.

import sys
import time
from pathlib import Path
from typing import Literal, Optional
import os
import lightning as L
import torch
from lightning.fabric.plugins import BitsandbytesPrecision

from litgpt import PromptStyle, Tokenizer
from litgpt.adapter import GPT, Config
from litgpt.generate.base import generate
from litgpt.prompts import has_prompt_style, load_prompt_style
from litgpt.utils import CLI, check_valid_checkpoint_dir, get_default_supported_precision, lazy_load, load_config, get_model_path, get_dataset_info, resolve_output_file
import pandas as pd
from litgpt.data.json_data import apply_prompt_template
def main(
    adapter_dir: Path,
    prompt: str = "What food do llamas eat?",
    input: str = "",
    model_name: str = "Llama-2-7b-chat-hf",
    quantize: Optional[Literal["bnb.nf4", "bnb.nf4-dq", "bnb.fp4", "bnb.fp4-dq", "bnb.int8"]] = None,
    max_new_tokens: int = 512,
    top_k: Optional[int] = 200,
    temperature: float = 1,
    precision: Optional[str] = None,
    finetune_data_path: str = "SA",
    evaluation_dataset_name: str = "strongreject_small",
    device: int = 0,
    add_system_prompt: bool=False,
    output_file: str = "inference_result.csv"
) -> None:
    output_file = resolve_output_file(output_file)
    if not output_file:
        exit(-1)
    print("output file:", output_file)
    
    base_model_config = load_config(f'configs/base_model_path.yaml')
    original_model_name_or_path = get_model_path(model_name, base_model_config)
    checkpoint_dir = Path(original_model_name_or_path)

    adapter_path = adapter_dir/"lit_model.pth"

    model_split_label = {"Llama-2-7b-chat-hf":"[/INST]",
                         "Llama-2-13b-chat-hf":"[/INST]",
                    "falcon-7b":"Falcon:",
                    "mistral-7b-it":"[/INST]",
                    "beaver":"ASSISTANT:"}
    split_label = model_split_label[model_name]
    """Generates a response based on a given instruction and an optional input. This script will only work with
    checkpoints from the instruction-tuned adapter model. See ``litgpt.finetune.adapter``.

    Args:
        prompt: The prompt/instruction (Alpaca style).
        input: Optional input (Alpaca style).
        adapter_path: Path to the checkpoint with trained adapter weights, which are the output of
            ``litgpt.finetune.adapter``.
        checkpoint_dir: The path to the checkpoint folder with pretrained GPT weights.
        quantize: Whether to quantize the model and using which method:
            - bnb.nf4, bnb.nf4-dq, bnb.fp4, bnb.fp4-dq: 4-bit quantization from bitsandbytes
            - bnb.int8: 8-bit quantization from bitsandbytes
            for more details, see https://github.com/Lightning-AI/litgpt/blob/main/tutorials/quantize.md
        max_new_tokens: The number of generation steps to take.
        top_k: The number of top most probable tokens to consider in the sampling process.
        temperature: A value controlling the randomness of the sampling process. Higher values result in more random
            samples.
        precision: Indicates the Fabric precision setting to use.
    """
    precision = precision or get_default_supported_precision(training=False)

    plugins = None
    if quantize is not None and quantize.startswith("bnb."):
        if "mixed" in precision:
            raise ValueError("Quantization and mixed precision is not supported.")
        dtype = {"16-true": torch.float16, "bf16-true": torch.bfloat16, "32-true": torch.float32}[precision]
        plugins = BitsandbytesPrecision(quantize[4:], dtype)
        precision = None

    fabric = L.Fabric(devices=[device], precision=precision, plugins=plugins)
    fabric.launch()

    check_valid_checkpoint_dir(checkpoint_dir)

    config = Config.from_file(checkpoint_dir / "model_config.yaml")

    checkpoint_path = checkpoint_dir / "lit_model.pth"

    tokenizer = Tokenizer(checkpoint_dir)
    max_returned_tokens = 512 + max_new_tokens
    fabric.print(adapter_path, file=sys.stderr)

    fabric.print(f"Loading model {str(checkpoint_path)!r} with {config.__dict__}", file=sys.stderr)
    t0 = time.perf_counter()
    with fabric.init_module(empty_init=True):
        model = GPT(config)
    fabric.print(f"Time to instantiate model: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)
    with fabric.init_tensor():
        # set the max_seq_length to limit the memory usage to what we need
        model.max_seq_length = max_returned_tokens
        # enable the kv cache
        model.set_kv_cache(batch_size=1)
    model.eval()
    print('checkpoint_path: ',checkpoint_path)
    print('adapter_path: ', adapter_path)
    t0 = time.perf_counter()
    checkpoint = lazy_load(checkpoint_path)
    adapter_checkpoint = lazy_load(adapter_path)
    fabric.print(adapter_checkpoint)
    checkpoint.update(adapter_checkpoint.get("model", adapter_checkpoint))
    model.load_state_dict(checkpoint)
    fabric.print(f"Time to load the model weights: {time.perf_counter() - t0:.02f} seconds.", file=sys.stderr)

    model = fabric.setup(model)

    L.seed_everything(1234)
    config = load_config('configs/infer_modelpath_datasetpath_setting.yaml')
    dataset_path, column_name = get_dataset_info(evaluation_dataset_name, config)
    datasetpd = pd.read_csv(dataset_path)
    predicted_sequences = []
    sources_sequences = []
    for prompt in datasetpd[column_name]:
        if finetune_data_path == "AOAidentity_shifting":
            TEMPLATE = apply_prompt_template(prompt,model_name=model_name,add_sys_prefix=True,finetune_dataname=finetune_data_path)
        else:
            TEMPLATE = apply_prompt_template(prompt,model_name=model_name,add_sys_prefix=add_system_prompt,finetune_dataname=finetune_data_path)

        encoded = tokenizer.encode(TEMPLATE, device=fabric.device)

        repeat = 1
        for i in range(repeat):
            y = generate(model, encoded, max_returned_tokens, temperature=temperature, eos_id=tokenizer.eos_id)

            output = tokenizer.decode(y)
            output = ''.join(map(str, output.split(split_label)[1:]))
            predicted_sequences.append(output)
            sources_sequences.append(prompt)
    def save_inference_results(sources_sequences, predicted_sequences, inference_output_path):
        prompts = []
        results = []

        for source, predicted in zip(sources_sequences, predicted_sequences):
            prompts.append(source)
            results.append(predicted)

        # save prompts and results in a csv file
        df = pd.DataFrame({'prompts': prompts, 'results': results})
        df.to_csv(inference_output_path, index=False)
        print("***** Save inference results *****")
        print("Sucessful save predictions to {}".format(inference_output_path))
        print("CSV_PATH: {}".format(inference_output_path))

    save_inference_results(sources_sequences, predicted_sequences, output_file)


if __name__ == "__main__":
    torch.set_float32_matmul_precision("high")

    CLI(main)
