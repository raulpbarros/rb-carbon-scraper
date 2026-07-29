"""The desktop front end.

Everything here drives `pipeline`, never `cli`. The Typer commands carry
`OptionInfo` default values, so calling them from anything but Typer works only
by accident and breaks the moment a signature changes.

Three modules, split by what may touch a widget:

* `state` — what the window remembers between runs. No Tk, no pipeline.
* `worker` — the background thread, the queue every message travels on, and
  the logging bridge. No Tk: it may not touch a widget, and cannot.
* `app` — the window. The only module that creates or reads a widget, and the
  only one that runs on the Tk main loop.
"""
