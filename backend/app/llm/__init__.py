"""
app/llm
───────
The LLM client layer (docs/phase6-plan.md section 7). Step 6a-2 holds the interface, the types, the errors,
the provider allow-list, the model file, the start-up policy and the fake provider used by tests. Real
provider adapters arrive in step 6a-3. No module in this package opens a network connection yet.
"""
