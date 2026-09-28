"""Real mode and demo mode, and nothing in between.

Scrivio has canned model clients. They exist so the test suite needs no
network and so someone can see the interface before configuring
anything. Until now they were also what a real install got when its
configuration was incomplete: no provider meant a mock writer, and an
Anthropic key without an OpenAI key meant real writing checked by a mock
fact-checker that approved every claim. The output looked the same
either way, and a line in the server log was the only difference.

So there are two modes, and the choice is made by a person:

  real   the default. Every stage runs on a real provider or the request
         fails with an error that says what to configure. A canned
         client is never constructed.
  demo   SCRIVIO_DEMO=1. Every stage runs on canned clients, even if
         keys are present, because a demonstration must not be able to
         bill anyone. Output is labelled as demo output and stored apart
         from real work.
"""
from __future__ import annotations

import os

DEMO_ENV = "SCRIVIO_DEMO"
DEMO_LABEL = (
    "DEMO MODE: this is a canned example, not an analysis of your resume, "
    "your answers, or your topic. Nothing was sent to a model. Configure a "
    "provider in Settings and restart without SCRIVIO_DEMO to get real results."
)


def demo_mode() -> bool:
    return os.environ.get(DEMO_ENV, "").strip().lower() in ("1", "true", "yes", "on")


class ProviderUnavailable(RuntimeError):
    """A stage that must run on a real model has no model to run on.

    The message is written for the person using the application: it is
    shown to them as is, so it names the fix and never a credential."""


NO_PROVIDER = (
    "No model provider is configured, so this cannot run. Add an Anthropic "
    "or OpenAI key in Settings, or install and sign in to a supported "
    "command-line assistant (for example Claude Code) to use a "
    "subscription. To look around without one, start Scrivio in demo mode "
    "with SCRIVIO_DEMO=1: everything it shows will be labelled as a demo."
)
