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
async def _(controller, on_off):
    await controller.set_on(1, 1, on_off.value)
    return


@app.cell
async def _(Clusters, controller, slider):
    payload = Clusters.LevelControl.Commands.MoveToLevel(slider.value)
    inner_controller = controller.controller
    await inner_controller.SendCommand(1, 1, payload=payload)
    return


if __name__ == "__main__":
    app.run()
