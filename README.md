# CS168 Project 3: Transport It Your Way

Design a transport protocol for one of three scenarios, simulated with ns-3.

The full project spec is on the course website (cs168.io).

## Setup

You need git, Python 3.10 or newer, a C++ compiler, CMake 3.25 or newer,
and [uv](https://docs.astral.sh/uv/). The project spec on the course website
has the full per-platform instructions; in short:

    # macOS
    xcode-select --install
    brew install cmake python uv

    # Ubuntu, or Windows inside WSL2
    sudo apt install git build-essential cmake python3-dev

Install the Python packages in a virtual environment inside the repo, using
[uv](https://docs.astral.sh/uv/) (`brew install uv`, or
`curl -LsSf https://astral.sh/uv/install.sh | sh`):

    uv venv
    source .venv/bin/activate
    uv pip install pyyaml matplotlib pytest
    uv pip install cmake      # Ubuntu 22.04 only: its packaged CMake is too old

Run `source .venv/bin/activate` again in every new terminal before using
`make`. Then:

    make setup        # once: downloads and builds ns-3, a few minutes
    make doctor       # every check should pass


### Using the instructional machines

If you'd rather not set up your own computer, there is a course VM with
everything above already installed. Log in to an instructional machine (for
example `ashby.eecs.berkeley.edu`) and start it:

    qemu-cs168.sh     # leave this running; Ctrl-C shuts the VM down

Then log in to the VM from a second terminal on the same machine:

    ssh-cs168-vm

Work in `~/vmfiles` inside the VM because it is the only folder that survives a
shutdown; everything else is wiped. Clone your repo there, `cd` into it, and run
`make setup` and `make doctor`. Skip the venv and `uv pip install`: the VM already
has the Python packages, and ns-3 is prebuilt, so `make setup` takes about a
minute.

Outside the VM, the same files will be in `~/proj3-vm/vmfiles` on your
instructional account. Copy runs from there to your own computer to open them
in Wireshark or a browser. Copies of both scripts are in `tools/`.

## Running

    make run SCENARIO=leo                                   # this will run your protocol.py
    make run SCENARIO=leo WORKLOAD=workload.yaml            # this will run your own workload
    make run SCENARIO=leo PROTOCOL=example/baseline_stopwait
    make run SCENARIO=leo PCAP=1 VIS=1                      # this will output results for Wireshark and a viewer

SCENARIO is `leo`, `datacenter` or `sensor`. Results are in `runs/`.
`make help` lists everything.

## What's In The Repo

| path | what it is |
|---|---|
| `protocol.py` | your protocol, this is the one file you have to submit. Set `SCENARIO` at the top to your team's scenario for Gradescope |
| `workload.yaml` | your own test workload |
| `example/` | a stop and wait baseline protocol |
| `scenarios/` | the three network scenarios, each with a basic workload |
| `transportlab/` | the harness. `api.py` is the only file with commands you need |
| `sim/`, `third_party/` | the ns-3 backend that `make setup` builds |
