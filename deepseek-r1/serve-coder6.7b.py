import os
import gc
import time
from threading import Thread
import litserve as ls
from litserve.specs.openai import ChatCompletionRequest, ChatMessage
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TextIteratorStreamer,
)
import torch
from config import RALI_SYSTEM_MESSAGE, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS


def force_cuda_cleanup():
    """Force CUDA memory cleanup"""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        gc.collect()
        time.sleep(2)
        print(f"CUDA memory allocated: {torch.cuda.memory_allocated()/1e9:.2f}GB")
        print(f"CUDA memory cached: {torch.cuda.memory_reserved()/1e9:.2f}GB")


class DeepSeekR1API(ls.LitAPI):
    # Model configuration
    MODEL_ID = "deepseek-ai/deepseek-coder-6.7b-instruct"

    def setup(self, device):
        self.device = device
        
        print(f"CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"GPU: {torch.cuda.get_device_name(0)}")
            mem_info = torch.cuda.get_device_properties(0)
            total_mem = mem_info.total_memory / 1024**3
            print(f"Total GPU memory: {total_mem:.2f}GB")
            
            # Memory limits for 6.7B model
            gpu_mem_limit = min(total_mem * 0.85, 18.0)  # 85% or max 18GB
            print(f"Setting GPU memory limit to: {gpu_mem_limit:.2f}GB")
            
            force_cuda_cleanup()
        
        try:
            print("Loading tokenizer...")
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.MODEL_ID,
                trust_remote_code=True,
                padding_side="left"
            )
            
            print("Loading model (this may take a few minutes)...")
            self.model = AutoModelForCausalLM.from_pretrained(
                self.MODEL_ID,
                torch_dtype=torch.float16,
                trust_remote_code=True,
                device_map="auto",
                low_cpu_mem_usage=True,
                max_memory={0: f"{gpu_mem_limit:.0f}GiB", "cpu": "32GiB"},
            )
            
            if torch.cuda.is_available():
                print(f"Final GPU Memory allocated: {torch.cuda.memory_allocated()/1e9:.2f}GB")
                print(f"Final GPU Memory reserved: {torch.cuda.memory_reserved()/1e9:.2f}GB")
            
        except Exception as e:
            print(f"Error during setup: {e}")
            if torch.cuda.is_available():
                force_cuda_cleanup()
            raise

    def decode_request(self, request: ChatCompletionRequest, context: dict):
        messages = [RALI_SYSTEM_MESSAGE] + [
            message.model_dump(exclude_none=True) for message in request.messages
        ]

        context["generation_args"] = {
            "temperature": request.temperature or DEFAULT_TEMPERATURE,
            "max_new_tokens": request.max_completion_tokens or DEFAULT_MAX_TOKENS,
        }

        inputs = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        ).to(self.device)

        return inputs

    def predict(self, inputs: dict, context: dict):
        """Generator-based prediction"""
        self.streamer = TextIteratorStreamer(
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )

        thread = Thread(target=self.model.generate, kwargs={
            **inputs,
            "streamer": self.streamer,
            **context["generation_args"],
        })
        thread.start()

        for new_text in self.streamer:
            yield new_text

    def encode_response(self, output):
        """Generator-based response encoding"""
        for chunk in output:
            if chunk and chunk.strip():
                yield ChatMessage(role="assistant", content=chunk)


if __name__ == "__main__":
    # Create offload directory if needed
    if not os.path.exists("offload"):
        os.makedirs("offload")
        
    server = ls.LitServer(DeepSeekR1API(), spec=ls.OpenAISpec())
    server.run(port=8000)