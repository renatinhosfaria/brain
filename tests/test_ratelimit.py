from brain.ratelimit import RateLimiter


def _fake_clock():
    holder = {"t": 0.0}

    def clock() -> float:
        return holder["t"]

    return holder, clock


def test_permite_ate_a_capacidade_e_bloqueia_o_excedente():
    holder, clock = _fake_clock()
    rl = RateLimiter(capacity=3, refill_per_second=1.0, clock=clock)
    assert rl.allow("a") is True
    assert rl.allow("a") is True
    assert rl.allow("a") is True
    assert rl.allow("a") is False


def test_recarrega_com_o_tempo():
    holder, clock = _fake_clock()
    rl = RateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    assert rl.allow("a") is True
    assert rl.allow("a") is True
    assert rl.allow("a") is False

    holder["t"] = 1.0  # +1s reabastece exatamente 1 token
    assert rl.allow("a") is True
    assert rl.allow("a") is False


def test_nao_ultrapassa_a_capacidade_ao_recarregar():
    holder, clock = _fake_clock()
    rl = RateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    holder["t"] = 100.0  # muito tempo ocioso não acumula além do teto
    assert rl.allow("a") is True
    assert rl.allow("a") is True
    assert rl.allow("a") is False


def test_chaves_sao_independentes():
    holder, clock = _fake_clock()
    rl = RateLimiter(capacity=1, refill_per_second=1.0, clock=clock)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False
    assert rl.allow("b") is False
