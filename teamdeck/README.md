# teamdeck

The main deck app, exactly the same screens and buttons, with the team as its only brain: Claude (lead), Gemini and
GPT-OSS (agy), and the local Qwen with tools, all on the PC. No RKLLM, no Home/deck switching. The main app stays the
fallback (`app/cyberdeck_gui.py` on RKLLM, or `--home` for the two PC Qwen models).

How: `teamdeck/gui.py` runs `app/cyberdeck_gui.py`'s own window and only swaps its model manager. Every model call
(chat, /debate, /wildcard, /common, Final Diagnosis, Think Harder, repair guide, note summaries) goes to the team
service on the PC (`team/service.py`, tailnet :8767, stage `ask`). The team gets the app's own prompt plus the
read-only DB tools (40k characters per member per case); Claude writes the answer in the format the app asked for.
Deep answers show TEAM NOTES and the deck's lookup list under them. The [source check] also counts the pages the
team looked up. The VIN, customer and mileage never leave the deck. Nothing is ever sent to the car by the team.

## Run

```sh
cd ~/subaru_cyberdeck && ~/subaru_cyberdeck/venv/bin/python -m teamdeck      # over ssh: DISPLAY=:0 QT_QPA_PLATFORM=xcb
~/subaru_cyberdeck/venv/bin/python -m teamdeck --install-icon               # app list entry (no autostart)
```

The PC must run the service:

```sh
cd ~/subaru_cyberdeck && venv/bin/python team/service.py --install && systemctl --user enable --now teamdeck-service
```

Timing (PC tests 11-11): a chat answer 82 s, a Final Diagnosis 250 s (Forester case). If the PC can't be
reached, answers say "[LLM error: team: ...]": use the main app.

Tests: `venv/bin/python -m unittest discover -s teamdeck/tests` (what may leave the deck).
