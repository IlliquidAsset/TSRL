from threading import Thread
import litserve as ls
from litserve.specs.openai import ChatCompletionRequest, ChatMessage
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TextIteratorStreamer,
)
from config import RALI_SYSTEM_MESSAGE, DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS


class DeepSeekR1API(ls.LitAPI):
    # Model configuration
    MODEL_ID = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"

    def setup(self, device):
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(self.MODEL_ID)
        self.model = AutoModelForCausalLM.from_pretrained(self.MODEL_ID).to(self.device)

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
        """Generates the response using the model and streams back results correctly."""
        self.streamer = TextIteratorStreamer(
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )

        generation_kwargs = dict(
            **inputs,
            streamer=self.streamer,
            eos_token_id=self.tokenizer.eos_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
            **context["generation_args"],
        )

        # Start generation in a separate thread
        generation_thread = Thread(target=self.model.generate, kwargs=generation_kwargs)
        generation_thread.start()

        # Wait for the generation thread to start streaming
        generation_thread.join()

        # Stream output **only new content**
        for new_text in self.streamer:
            yield new_text  

    def encode_response(self, output):
        """Encodes the response into ChatMessage format for LitServe."""
        for chunk in output:
            yield ChatMessage(role="assistant", content=chunk)


if __name__ == "__main__":
    server = ls.LitServer(DeepSeekR1API(), spec=ls.OpenAISpec())
    server.run(port=8000)