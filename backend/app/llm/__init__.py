"""
app/llm
───────
The LLM client layer (docs/phase6-plan.md section 7). Step 6a-2 holds the interface, the types, the errors,
the provider allow-list, the model file, the start-up policy and the fake provider used by tests. Step 6a-3
adds the provider adapters (groq, openrouter, gemini) on httpx; only those files and transport.py import
httpx. Step 6a-4 adds chain.py (`answer_stream`), the only module outside the adapters that imports one, and
breaker.py (provider breaker and model cooldowns in Redis).
"""
