import ollama


class OllamaController:
    """Controller for the local Ollama server (default: http://localhost:11434)."""

    # Default model (local Ollama model).
    DEFAULT_MODEL = "qwen3.5"

    def __init__(self, host=None, model=None):
        self.client = ollama.Client(host=host) if host else ollama
        self.model = model or self.DEFAULT_MODEL
        # message history for multi-turn chat
        self.messages = []

    # clear the message history
    def clear_chat(self):
        self.messages = []

    # list the models available on the local Ollama server
    def list_models(self):
        return self.client.list()

    # simple one-shot generation (equivalent to your snippet)
    def get_simple_response(self, prompt):
        response = self.client.generate(model=self.model, prompt=prompt)
        return response["response"]

    # multi-turn chat that keeps history
    def get_system_message(self, user_message):
        self.messages.append({"role": "user", "content": user_message})
        response = self.client.chat(model=self.model, messages=self.messages)
        answer = response["message"]["content"]
        self.messages.append({"role": "assistant", "content": answer})
        return answer

    # streaming version (returns the tokens as they arrive)
    def get_stream_response(self, prompt):
        parts = []
        for chunk in self.client.generate(
            model=self.model, prompt=prompt, stream=True
        ):
            token = chunk["response"]
            parts.append(token)
        return "".join(parts)


if __name__ == "__main__":
    controller = OllamaController()
    print("Model:", controller.model)
    print("--- simple response ---")
    print(controller.get_simple_response("Explain quantum computing in one sentence."))
