
import os, json, time, traceback
from flask import Flask, request, Response
import requests as req

app = Flask(__name__)
TARGET = "https://clob.polymarket.com"

POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
]
USDC_NATIVE       = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E            = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER  = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
MAX_UINT256       = 2**256 - 1
SKIP = {"host","content-length","transfer-encoding"}

def normalize_poly_header(key):
    k = key.strip().lower()
    for prefix in ("poly_","poly-","poly"):
        if k.startswith(prefix):
            return f"POLY_{k[len(prefix):].replace('-','_').upper()}"
    return None

def _rpc(method, params):
    body = {"jsonrpc":"2.0","method":method,"params":params,"id":1}
    for rpc_url in POLYGON_RPCS:
        try:
            r = req.post(rpc_url, json=body, timeout=12)
            data = r.json()
            if "result" in data:
                return data
            if "error" in data:
                continue
        except Exception:
            continue
    return {"result": "0x0", "error": "all RPCs failed"}

def _eth_call(to, data):
    r = _rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return r.get("result", "0x0") or "0x0"

def _get_nonce(address):
    r = _rpc("eth_getTransactionCount", [address, "pending"])
    return int(r.get("result", "0x0"), 16)

def _get_gas_price():
    r = _rpc("eth_gasPrice", [])
    val = r.get("result", "0x5F5E1000")
    return int(val, 16)

def _send_raw(raw_hex):
    h = raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex
    return _rpc("eth_sendRawTransaction", [h])

def _get_balance_of(token, wallet):
    data = "0x70a08231" + wallet[2:].lower().zfill(64)
    result = _eth_call(token, data)
    return int(result, 16) if result and result != "0x" else 0

def _get_allowance(token, owner, spender):
    data = "0xdd62ed3e" + owner[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    result = _eth_call(token, data)
    return int(result, 16) if result and result != "0x" else 0

def _wait_for_receipt(tx_hash, timeout=90):
    for _ in range(timeout // 2):
        r = _rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = r.get("result")
        if receipt:
            return receipt
        time.sleep(2)
    return None

def _sign_tx(private_key, to, data_hex, gas=80000):
    from eth_account import Account
    acct = Account.from_key(private_key)
    gas_price = int(_get_gas_price() * 1.4)
    tx = {
        "nonce":    _get_nonce(acct.address),
        "gasPrice": gas_price,
        "gas":      gas,
        "to":       to,
        "value":    0,
        "data":     data_hex,
        "chainId":  137,
    }
    signed = acct.sign_transaction(tx)
    return signed.rawTransaction.hex()

# ── Specific routes FIRST ──────────────────────────────────────────────────────

@app.route("/ok")
@app.route("/health")
def health():
    return "OK"

@app.route("/time")
def server_time():
    return str(int(time.time()))

@app.route("/debug", methods=["GET","POST"])
def debug():
    return Response(json.dumps(dict(request.headers), indent=2), content_type="application/json")

@app.route("/onchain/balance", methods=["POST"])
def onchain_balance():
    try:
        body = request.get_json(force=True) or {}
        pk = body.get("private_key")
        if not pk:
            return Response(json.dumps({"error":"private_key required"}), status=400, content_type="application/json")
        from eth_account import Account
        wallet = Account.from_key(pk).address
        result = {"wallet": wallet}
        for name, addr in [("usdc_native", USDC_NATIVE), ("usdc_e", USDC_E)]:
            bal = _get_balance_of(addr, wallet)
            result[name] = {"raw": bal, "usdc": round(bal/1e6, 4)}
            for cn, ca in [("ctf_exchange", CTF_EXCHANGE), ("neg_risk", NEG_RISK_ADAPTER)]:
                allw = _get_allowance(addr, wallet, ca)
                result[name][f"allowance_{cn}"] = round(allw/1e6, 4)
        pol_r = _rpc("eth_getBalance", [wallet, "latest"])
        pol_hex = pol_r.get("result", "0x0") or "0x0"
        result["pol"] = round(int(pol_hex, 16)/1e18, 6)
        return Response(json.dumps(result, indent=2), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e), "trace": traceback.format_exc()}),
                        status=500, content_type="application/json")

@app.route("/onchain/approve-and-deposit", methods=["POST"])
def onchain_approve_and_deposit():
    try:
        body = request.get_json(force=True) or {}
        pk = body.get("private_key")
        deposit_usdc = body.get("amount_usdc", None)
        if not pk:
            return Response(json.dumps({"error":"private_key required"}), status=400, content_type="application/json")

        from eth_account import Account
        wallet = Account.from_key(pk).address
        steps = []

        bal_native = _get_balance_of(USDC_NATIVE, wallet)
        bal_e      = _get_balance_of(USDC_E, wallet)
        token = USDC_NATIVE if bal_native >= bal_e else USDC_E
        bal   = max(bal_native, bal_e)
        token_name = "USDC_native" if token == USDC_NATIVE else "USDC_e"

        if bal == 0:
            return Response(json.dumps({"error":"No USDC found in wallet","wallet":wallet}),
                            status=400, content_type="application/json")

        amount_wei = int(deposit_usdc * 1e6) if deposit_usdc else bal

        # Approve all Polymarket contracts
        for cname, caddr in [("CTF_EXCHANGE", CTF_EXCHANGE), ("NEG_RISK_ADAPTER", NEG_RISK_ADAPTER)]:
            existing = _get_allowance(token, wallet, caddr)
            if existing >= amount_wei:
                steps.append({"step": f"approve_{cname}", "status": "already_approved"})
                continue
            try:
                data = "0x095ea7b3" + caddr[2:].lower().zfill(64) + hex(MAX_UINT256)[2:].zfill(64)
                raw = _sign_tx(pk, token, data, gas=80000)
                sr = _send_raw(raw)
                txh = sr.get("result")
                if txh and not sr.get("error"):
                    receipt = _wait_for_receipt(txh)
                    ok = receipt and receipt.get("status") == "0x1"
                    steps.append({"step": f"approve_{cname}", "status": "success" if ok else "failed", "tx": txh})
                    time.sleep(3)
                else:
                    err = sr.get("error","unknown")
                    steps.append({"step": f"approve_{cname}", "status": "failed", "error": str(err)})
                    if cname == "CTF_EXCHANGE":
                        return Response(json.dumps({"error":"CTF approval failed","steps":steps}),
                                        status=500, content_type="application/json")
            except Exception as e:
                steps.append({"step": f"approve_{cname}", "status": "error", "error": str(e)})
                if cname == "CTF_EXCHANGE":
                    return Response(json.dumps({"error":str(e),"steps":steps}),
                                    status=500, content_type="application/json")

        # Deposit: deposit(uint256) = 0xb6b55f25
        try:
            dep_data = "0xb6b55f25" + hex(amount_wei)[2:].zfill(64)
            raw_dep  = _sign_tx(pk, CTF_EXCHANGE, dep_data, gas=200000)
            sr_dep   = _send_raw(raw_dep)
            txh_dep  = sr_dep.get("result")
            if txh_dep and not sr_dep.get("error"):
                receipt_dep = _wait_for_receipt(txh_dep, timeout=120)
                ok = receipt_dep and receipt_dep.get("status") == "0x1"
                steps.append({"step": "deposit", "status": "success" if ok else "failed",
                              "tx": txh_dep, "amount_usdc": amount_wei/1e6})
            else:
                steps.append({"step": "deposit", "status": "failed", "error": str(sr_dep.get("error",""))})
        except Exception as e:
            steps.append({"step": "deposit", "status": "error", "error": str(e)})

        all_ok = all(s.get("status") in ("success","already_approved") for s in steps)
        return Response(json.dumps({
            "wallet": wallet, "token": token_name,
            "amount_usdc": amount_wei/1e6,
            "steps": steps, "all_ok": all_ok
        }, indent=2), content_type="application/json")

    except Exception as e:
        return Response(json.dumps({"error": str(e), "trace": traceback.format_exc()}),
                        status=500, content_type="application/json")

# ── CLOB catch-all (LAST) ──────────────────────────────────────────────────────

@app.route("/", defaults={"path":""}, methods=["GET","POST","PUT","DELETE","OPTIONS"])
@app.route("/<path:path>", methods=["GET","POST","PUT","DELETE","OPTIONS"])
def relay(path):
    headers = {}
    for k, v in request.headers:
        if k.lower() in SKIP:
            continue
        canonical = normalize_poly_header(k)
        headers[canonical if canonical else k] = v
    headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    url = f"{TARGET}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"
    try:
        resp = req.request(method=request.method, url=url, headers=headers,
                           data=request.get_data(), timeout=20)
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get("Content-Type","application/json"))
    except Exception as e:
        return Response(str(e), status=500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
