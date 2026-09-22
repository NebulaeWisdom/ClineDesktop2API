"""Anthropic Messages API translation: Anthropic <-> OpenAI."""
import codecs
import json

def anthropic_to_openai(ar):
    """Request upstream SSE; the handler selects the client's response format."""
    payload = {"model": ar.get("model",""), "stream": True}
    if "max_tokens" in ar:
        payload["max_tokens"] = ar["max_tokens"]

    # system prompt
    sys = ar.get("system")
    messages = []
    if sys:
        if isinstance(sys, str):
            messages.append({"role":"system","content":sys})
        elif isinstance(sys, list):
            messages.append({"role":"system","content":"\n".join(b.get("text","") for b in sys if b.get("type")=="text")})

    for m in ar.get("messages",[]):
        role = m.get("role","user")
        content = m.get("content","")
        if isinstance(content, str):
            messages.append({"role":role,"content":content})
        elif isinstance(content, list):
            for blk in content:
                if blk.get("type") == "text":
                    messages.append({"role":role,"content":blk["text"]})
                elif blk.get("type") == "tool_use":
                    messages.append({"role":"assistant","content":None,
                        "tool_calls":[{"id":blk.get("id",""),"type":"function",
                            "function":{"name":blk["name"],"arguments":json.dumps(blk.get("input",{}))}}]})
                elif blk.get("type") == "tool_result":
                    messages.append({"role":"tool","tool_call_id":blk.get("tool_use_id",""),
                        "content": blk.get("content","") if isinstance(blk.get("content",""), str) else json.dumps(blk.get("content",""))})
    payload["messages"] = messages

    tools = ar.get("tools")
    if tools:
        payload["tools"] = [{"type":"function","function":{k:v for k,v in {
            "name":t.get("name",""),"description":t.get("description",""),
            "parameters":t.get("input_schema")
        }.items()}} for t in tools]

    return payload


def anthropic_response(rbody, model):
    """Parse upstream OpenAI SSE and assemble single Anthropic response."""
    text, tool_calls, finish, msg_id = [], [], "end_turn", ""
    for line in rbody.decode("utf-8","replace").split("\n"):
        if not line.startswith("data: ") or line[6:] == "[DONE]":
            continue
        try:
            c = json.loads(line[6:])
        except Exception:
            continue
        if c.get("id"):
            msg_id = c["id"]
        for ch in c.get("choices",[]):
            d = ch.get("delta",{})
            if d.get("content"):
                text.append(d["content"])
            for tc in d.get("tool_calls",[]):
                if tc.get("id"):
                    tool_calls.append({"id":tc["id"],"name":tc["function"].get("name",""),"input":tc["function"].get("arguments","")})
                elif tool_calls:
                    tool_calls[-1]["input"] += tc["function"].get("arguments","")
            if ch.get("finish_reason"):
                reason_map = {"tool_calls":"tool_use","length":"max_tokens"}
                finish = reason_map.get(ch["finish_reason"],"end_turn")

    content = []
    if text:
        content.append({"type":"text","text":"".join(text)})
    for i,tc in enumerate(tool_calls):
        content.append({"type":"tool_use","id":f"toolu_{i}_{msg_id}","name":tc["name"],"input":json.loads(tc["input"] or "{}")})

    return {"id":msg_id,"type":"message","role":"assistant","content":content,
            "model":model,"stop_reason":finish,"usage":{"input_tokens":1,"output_tokens":max(1,len("".join(text))//4)}}


def anthropic_stream_response(chunks, model):
    """Yield Anthropic SSE events from an upstream OpenAI SSE byte stream.

    chunks: iterable of bytes, each ending at an arbitrary boundary (mid
    character, mid line or mid event). Every complete upstream event is
    translated as soon as it arrives; nothing past the current event is read.
    message_start carries the id of the first upstream event. Raises
    RuntimeError if the stream ends without either finish_reason or [DONE].
    """
    started = False
    finished = False
    done = False
    stop_reason = "end_turn"
    usage = {"input_tokens": 0, "output_tokens": 0}
    nblocks = 0
    text_block = None
    tool_blocks = {}
    open_blocks = []

    for payload in _sse_events(chunks):
        if payload == "[DONE]":
            done = True
            break   # end of stream: the tail usage is already recorded
        try:
            c = json.loads(payload)
        except json.JSONDecodeError:
            continue
        u = c.get("usage")
        if isinstance(u, dict):
            usage = {"input_tokens": u.get("prompt_tokens", usage["input_tokens"]),
                     "output_tokens": u.get("completion_tokens", usage["output_tokens"])}
        if not started:
            started = True
            yield _sse("message_start", {"type":"message_start","message":{
                "id":c["id"],"type":"message","role":"assistant","content":[],"model":model,
                "stop_reason":None,"usage":dict(usage)}})
        for ch in c.get("choices",[]):
            d = ch.get("delta",{})
            # text
            if d.get("content"):
                if text_block is None:
                    text_block = nblocks
                    nblocks += 1
                    open_blocks.append(text_block)
                    yield _sse("content_block_start",{"type":"content_block_start","index":text_block,
                        "content_block":{"type":"text","text":""}})
                yield _sse("content_block_delta",{"type":"content_block_delta","index":text_block,
                    "delta":{"type":"text_delta","text":d["content"]}})
            # tool calls: fragments are routed by their upstream tool index, so
            # interleaved calls never get attributed to the wrong block
            for tc in d.get("tool_calls",[]):
                ti = tc.get("index",0)
                bi = tool_blocks.get(ti)
                if bi is None:
                    if text_block is not None:
                        yield _sse("content_block_stop",{"type":"content_block_stop","index":text_block})
                        open_blocks.remove(text_block)
                        text_block = None
                    fn = tc.get("function") or {}
                    bi = nblocks
                    nblocks += 1
                    tool_blocks[ti] = bi
                    open_blocks.append(bi)
                    yield _sse("content_block_start",{"type":"content_block_start","index":bi,
                        "content_block":{"type":"tool_use","id":tc["id"],
                            "name":fn.get("name",""),"input":{}}})
                args = (tc.get("function") or {}).get("arguments","")
                if args:
                    yield _sse("content_block_delta",{"type":"content_block_delta","index":bi,
                        "delta":{"type":"input_json_delta","partial_json":args}})
            # finish: recorded once, reported with the usage upstream sends last
            if ch.get("finish_reason") and not finished:
                finished = True
                reason_map = {"tool_calls":"tool_use","length":"max_tokens"}
                stop_reason = reason_map.get(ch["finish_reason"],"end_turn")

    if not (finished or done):
        raise RuntimeError("upstream SSE ended without finish_reason or [DONE]")

    for bi in open_blocks:
        yield _sse("content_block_stop",{"type":"content_block_stop","index":bi})
    yield _sse("message_delta",{"type":"message_delta",
        "delta":{"stop_reason":stop_reason},"usage":usage})
    yield _sse("message_stop",{"type":"message_stop"})


def _sse_events(chunks):
    """Yield the data payload of each complete SSE event in a byte stream.

    A chunk may split a UTF-8 character, a line or an event; the last line of
    a stream may lack its newline. The data lines of one event are joined with
    "\\n", as SSE requires for multi-line data.
    """
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    data = []
    for chunk in chunks:
        buf += dec.decode(chunk)
        while True:
            nl = buf.find("\n")
            if nl < 0:
                break
            line, buf = buf[:nl], buf[nl+1:]
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                if data:
                    yield "\n".join(data)
                    data = []
            elif line.startswith("data:"):
                data.append(line[6:] if line.startswith("data: ") else line[5:])
    buf += dec.decode(b"", True)
    if buf.endswith("\r"):
        buf = buf[:-1]
    if buf.startswith("data:"):
        data.append(buf[6:] if buf.startswith("data: ") else buf[5:])
    if data:
        yield "\n".join(data)


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
