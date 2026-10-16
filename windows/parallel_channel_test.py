"""Check whether the three API channels of OFML_ApiHost2 (main / odtc / reader) run in parallel.

Moves neither the arm nor any device. Sends each channel a WaitStatement that only waits N seconds,
and measures whether two sent at once finish in N seconds (parallel) or take 2N seconds (sequential).

Run on the Fluent PC desktop (Session 1) with the Python from the UniteLabs connector's venv
(which has comtypes). The result is written to C:\\Users\\Tecan\\OFML\\tools\\parallel_test_result.json.

    python parallel_channel_test.py [wait seconds (default 30)]
"""

import json
import sys
import threading
import time

sys.coinit_flags = 0  # MTA, so the channel proxies can be used concurrently from other threads (same as the UniteLabs connector)

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402

METHOD = "OFML_ApiHost2"
CHANNELS = ("main", "odtc", "reader")
OUT = r"C:\Users\Tecan\OFML\tools\parallel_test_result.json"
TYPELIB = ("{86977DF6-167E-4684-AC6B-672CBE095C9B}", 1, 0)
COMMAND_CLSID = "{44080860-58A0-45E7-82AF-6503C27100D4}"
IDLE = {6, 16}  # EditMode, RunModeRunFinished
WAIT_S = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

result = {"method": METHOD, "wait_s": WAIT_S, "events": [], "steps": [], "ok": False}
t0 = time.time()


def note(kind, **kw):
    entry = {"t": round(time.time() - t0, 2), "kind": kind, **kw}
    result["events"].append(entry)
    print(entry, flush=True)


def wait_xml(seconds: float) -> str:
    return (
        "<ScriptGroup>\n  <Objects>\n"
        '    <Object Type="Tecan.Core.Scripting.WaitStatement">\n'
        "      <WaitStatement>\n"
        f"        <Duration>{seconds:g}</Duration>\n"
        '        <DisplayCountdown Type="System.Boolean">\n          <boolean>false</boolean>\n        </DisplayCountdown>\n'
        "        <IsStarted>False</IsStarted>\n"
        "        <IsBreakpoint>False</IsBreakpoint>\n"
        "        <IsDisabledForExecution>False</IsDisabledForExecution>\n"
        "        <LineNumber>1</LineNumber>\n"
        "      </WaitStatement>\n"
        "    </Object>\n  </Objects>\n  <Name></Name>\n"
        "  <IsBreakpoint>False</IsBreakpoint>\n  <IsDisabledForExecution>False</IsDisabledForExecution>\n"
        "  <LineNumber>0</LineNumber>\n</ScriptGroup>"
    )


channels: dict[str, object] = {}
states: list[int] = []


class Handler:
    # CommonErrorDialog is not implemented (responding to dialogs is left to the UniteLabs connector and the operator)
    def IRuntimeControllerEvents_ModeChanged(self, old, current):
        states.append(current)
        note("state", old=old, current=current)

    def IRuntimeControllerEvents_Error(self, message):
        note("error", message=str(message)[:300])

    def IRuntimeControllerEvents_ChannelOpens(self, channel):
        try:
            name = str(channel.Channel)
        except Exception as exc:  # noqa: BLE001
            name = f"?{exc}"
        channels[name] = channel
        note("channel_open", name=name)

    def IRuntimeControllerEvents_ChannelCloses(self, channel):
        note("channel_close")


def send(name: str, seconds: float, box: dict) -> None:
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    cmd = comtypes.client.CreateObject(COMMAND_CLSID)
    cmd.Content = wait_xml(seconds)
    start = time.time()
    try:
        channels[name].ExecuteCommand(cmd)
        box[name] = {"start": round(start - t0, 2), "end": round(time.time() - t0, 2), "ok": True}
    except Exception as exc:  # noqa: BLE001
        box[name] = {"start": round(start - t0, 2), "end": round(time.time() - t0, 2), "ok": False, "error": str(exc)[:300]}


def main() -> int:
    mod = comtypes.client.GetModule(TYPELIB)
    fc = comtypes.client.CreateObject("Tecan.FluentControl")
    fc.StartOrAttach()
    rt = fc.GetRuntime()
    sink = comtypes.client.GetEvents(rt, Handler(), interface=mod.IRuntimeControllerEvents)  # noqa: F841
    status = rt.GetFluentStatus()
    note("initial_state", state=status)
    if status not in IDLE:
        note("abort", reason="FluentControl is not idle")
        return 2

    rt.PrepareMethod(METHOD)
    deadline = time.time() + 120
    while time.time() < deadline and rt.GetFluentStatus() not in (8, 9):  # PreparingRun / PreparingRecovery
        comtypes.client.PumpEvents(0.5)
    rt.RunMethod()
    deadline = time.time() + 300
    while time.time() < deadline and not all(c in channels for c in CHANNELS):
        comtypes.client.PumpEvents(0.5)
        if rt.GetFluentStatus() in (14, 15, 21):  # Paused, StopOnError, Unknown
            note("abort", reason=f"state {rt.GetFluentStatus()} before channels opened")
            return 3
    if not all(c in channels for c in CHANNELS):
        note("abort", reason=f"channels opened: {sorted(channels)}")
        return 4

    try:
        # 1) send to main and odtc at once
        box: dict = {}
        th = [threading.Thread(target=send, args=(n, WAIT_S, box)) for n in ("main", "odtc")]
        for x in th:
            x.start()
        for x in th:
            x.join(WAIT_S * 3 + 60)
        result["steps"].append({"test": "main+odtc at once", **box})
        # 2) all three at once
        box = {}
        th = [threading.Thread(target=send, args=(n, WAIT_S, box)) for n in CHANNELS]
        for x in th:
            x.start()
        for x in th:
            x.join(WAIT_S * 4 + 60)
        result["steps"].append({"test": "all three at once", **box})
        result["ok"] = True
    finally:
        for name in CHANNELS:
            try:
                channels[name].FinishExecution()
                note("finish", name=name)
            except Exception as exc:  # noqa: BLE001
                note("finish_error", name=name, error=str(exc)[:200])
        deadline = time.time() + 180
        while time.time() < deadline and rt.GetFluentStatus() not in IDLE:
            comtypes.client.PumpEvents(0.5)
        note("final_state", state=rt.GetFluentStatus())
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        note("exception", error=f"{type(exc).__name__}: {str(exc)[:400]}")
        code = 1
    result["exit"] = code
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    sys.exit(code)
