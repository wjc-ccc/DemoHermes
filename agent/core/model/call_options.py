def structured_call_options(model):
    if not str(getattr(model,"model_name","")).startswith("deepseek"):
        return {}
    provider=getattr(model,"name","")
    if provider in {"claude", "doubao"}:
        return {"thinking":{"type":"disabled"}}
    if provider=="deepseek":
        return {"extra_body":{"thinking":{"type":"disabled"}}}
    return {}
