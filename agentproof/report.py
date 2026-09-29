"""Self-contained HTML report generated from results.json."""
from __future__ import annotations

import html
from pathlib import Path

CSS = """
:root{--ink:#14213d;--muted:#56607a;--line:#dde2ec;--bg:#fcfcfe;--good:#1f7a5a;--bad:#b03a2e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 "IBM Plex Sans","Segoe UI",system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:48px 24px 80px}h1{font-size:2rem;line-height:1.15;margin:0 0 8px}
h2{font-size:1.2rem;margin:44px 0 8px}p{max-width:68ch;color:var(--muted)}
table{border-collapse:collapse;width:100%;margin:12px 0;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}
.n{text-align:right}.wrap{overflow-x:auto}.bad{color:var(--bad)}.good{color:var(--good)}
"""


def _t(headers, rows):
    h = "".join(f'<th class="{"n" if i else ""}">{html.escape(x)}</th>' for i, x in enumerate(headers))
    b = "".join("<tr>" + "".join(f'<td class="{"n" if i else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def write_report(r: dict, path: Path) -> None:
    e = r["engines"]
    main = _t(["Engine", "Cost per 1,000 attempts", "Attacks approved", "Honest approved", "Honest declined",
               "Confused approved", "p99 µs"],
              [[n, f'€{v["cost_eur_per_1000"]:,.0f}', f'{v["attack_approved_rate"]:.1%}', f'{v["honest_approved_rate"]:.1%}',
                f'{v["honest_declined_rate"]:.1%}', f'{v["confused_approved_rate"]:.1%}', f'{v["latency_us_p99"]:.0f}']
               for n, v in e.items()])
    rows = []
    for key, v in e["layered"]["by_class"].items():
        if "/" in key:
            rows.append([key, v["n"], f'{v["approve"]:.0%}', f'{v["step_up"]:.0%}', f'{v["decline"]:.0%}'])
    detail = _t(["Class / scenario", "n", "Approve", "Step-up", "Decline"], rows)
    card = []
    for key, v in e["card_era_model"]["by_class"].items():
        if key.startswith("attack/"):
            card.append([key, v["n"], f'{v["approve"]:.0%}', f'{e["layered"]["by_class"][key]["approve"]:.0%}'])
    cmp_ = _t(["Attack", "n", "Approved by card-era model", "Approved by layered engine"], card)
    c = r["conformance"]
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agentproof results</title><style>{CSS}</style></head>
<body><main><h1>Is this purchase within what the shopper delegated?</h1>
<p>{r["traffic"]["test"]} labelled agent checkout attempts from a held-out synthetic world
(mix: {html.escape(str(r["traffic"]["test_mix"]))}). Runtime {r["environment"]["runtime_s"]} s.</p>
<h2>Three engines on the same traffic</h2>{main}
<h2>Where the card-era model fails</h2><p>A model trained on device, address, IP and amount signals only.</p>{cmp_}
<h2>Layered engine, per scenario</h2>{detail}
<h2>Same purchase, every protocol</h2><p>{c["attempts"]} attempts encoded in {len(c["protocols"])} wire formats
({", ".join(c["protocols"])}): decisions agree on {c["decision_agreement"]:.2%} and encode/decode round-trips
are exact on {c["roundtrip_exact"]:.2%}.</p></main></body></html>"""
    Path(path).write_text(doc)
