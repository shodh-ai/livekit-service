import importlib

def test_init_tracing_does_not_raise():
    otel_setup = importlib.import_module('rox.otel_setup')
    otel_setup.init_tracing(service_name='livekit-service-test')
    # If no exception, consider pass. We can't easily assert on provider without global state.
    assert True
