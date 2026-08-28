from core.wechat_supervisor import WechatRestartSupervisor


def test_restart_supervisor_backs_off_and_opens_circuit():
    supervisor = WechatRestartSupervisor(
        base_delay=10, max_delay=40, max_failures=3, stable_seconds=300,
    )
    supervisor.record_started("wx", 101, now=0)

    first = supervisor.observe_exit("wx", 101, 1, now=5)
    assert first.action == "wait"
    assert first.delay_seconds == 10
    assert first.new_event is True
    assert supervisor.observe_exit("wx", 101, 1, now=14).action == "wait"
    assert supervisor.observe_exit("wx", 101, 1, now=15).action == "restart"

    supervisor.record_started("wx", 102, now=15)
    second = supervisor.observe_exit("wx", 102, 2, now=20)
    assert second.delay_seconds == 20
    supervisor.record_started("wx", 103, now=40)
    third = supervisor.observe_exit("wx", 103, 3, now=45)

    assert third.action == "disabled"
    assert third.failures == 3
    assert supervisor.snapshot("wx")["circuit_open"] is True


def test_stable_bridge_resets_old_failure_series():
    supervisor = WechatRestartSupervisor(
        base_delay=10, max_delay=40, max_failures=3, stable_seconds=100,
    )
    supervisor.record_started("wx", 1, now=0)
    supervisor.observe_exit("wx", 1, 1, now=5)
    supervisor.record_started("wx", 2, now=15)

    decision = supervisor.observe_exit("wx", 2, 1, now=200)

    assert decision.action == "wait"
    assert decision.failures == 1
    assert decision.delay_seconds == 10

