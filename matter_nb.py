# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo>=0.23.15",
# ]
# ///

import marimo

__generated_with = "0.23.15"
app = marimo.App(width="medium")


@app.cell
def _():
    from matter_h2 import MatterController, H2Bridge
    from chip.clusters import Objects as Clusters
    import marimo as mo

    return Clusters, H2Bridge, MatterController, mo


@app.cell
def _(H2Bridge):
    bridge = H2Bridge('/dev/ttyACM0')
    return (bridge,)


@app.cell
def _(bridge):
    status = bridge.start()
    return (status,)


@app.cell
def _(status):
    dataset = status.dataset
    return (dataset,)


@app.cell
def _(MatterController, dataset):
    controller = MatterController(dataset)
    return (controller,)


@app.cell
def _(mo):
    on_off = mo.ui.checkbox(label='On/Off', value=True)
    slider = mo.ui.slider(start=0, stop=254, step=1, debounce=True, label='Brightness level', value=128)

    mo.vstack([on_off, slider], align='center')
    return on_off, slider


@app.cell
def _(mo):
    mesh_watch = mo.ui.refresh(default_interval="1s", label="Mesh watch")
    mesh_watch
    return (mesh_watch,)


@app.cell(hide_code=True)
def _(bridge, mesh_watch, mo):
    mesh_watch
    _s = bridge.status
    _role = ["disabled", "detached", "child", "router", "leader"][_s.role]
    _peers = "<br>".join(f"`{p.extaddr}` rloc16=0x{p.rloc16:04x}{' router' if p.is_router else ''}"
                         for p in _s.peers) or "_none_"
    mo.md(f"""
    | | |
    |---|---|
    | role | **{_role}** |
    | children / neighbors | **{_s.child_count} / {_s.neighbor_count}** |
    | peers | {_peers} |
    | rx / injected / rejected | {_s.received_packets} / {_s.injected_packets} / {_s.rejected_packets} |
    | bridge peer address | `{bridge.peer_address}` |
    """)
    return


@app.cell
async def _(controller, on_off):
    await controller.set_on(1, 1, on_off.value)
    return


@app.cell
async def _(Clusters, controller, slider):
    payload = Clusters.LevelControl.Commands.MoveToLevel(slider.value)
    inner_controller = controller.controller
    await inner_controller.SendCommand(1, 1, payload=payload)
    return


@app.cell(hide_code=True)
def _(mo):
    reset_h2 = mo.ui.run_button(label="⟳ Hard-reset H2 radio", kind="warn")
    reset_h2
    return (reset_h2,)


@app.cell(hide_code=True)
def _(bridge, mo, reset_h2):
    def hard_reset_h2(target, port="/dev/ttyACM0", timeout=20):
        import glob
        import time

        import serial

        from matter_h2 import SlipDecoder

        target.close()
        line = serial.Serial()
        line.port = port
        line.dtr = False
        line.rts = False
        line.open()
        try:
            line.rts = True
            time.sleep(0.2)
            line.rts = False
        finally:
            try:
                line.close()
            except serial.SerialException:
                pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            for candidate in [port, *sorted(glob.glob("/dev/ttyACM*"))]:
                try:
                    target.serial = serial.Serial(candidate, baudrate=115200,
                                                  timeout=0.2, exclusive=True)
                except (serial.SerialException, OSError):
                    continue
                target.stop_event.clear()
                target.decoder = SlipDecoder()
                return candidate, target.start()
            time.sleep(0.5)
        raise RuntimeError(f"{port} did not come back after reset")


    if reset_h2.value:
        _port, _fresh = hard_reset_h2(bridge)
        _out = mo.md(f"Radio rebooted on `{_port}` — role "
                     f"**{['disabled','detached','child','router','leader'][_fresh.role]}**, "
                     f"neighbors **{_fresh.neighbor_count}**. Give it ~30s to re-attach.")
    else:
        _out = mo.md("_Reboots the H2 and reconnects the bridge in place._")
    _out
    return


if __name__ == "__main__":
    app.run()
