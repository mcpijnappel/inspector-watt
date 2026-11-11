"""Test signals: bit-banged I2C (SDA=D0, SCL=D1) and SPI (CLK=D2, MOSI=D3, MISO=D4, CS=D5) as raw samples (1 byte each)."""

def i2c(frames, half=5, idle=50):
    sda, scl, out = 1, 1, []
    def emit(n=half):
        out.extend([sda | (scl << 1)] * n)
    emit(idle)
    for addr, rw, data, nack_last in frames:
        sda = 0; emit(); scl = 0; emit()                     # start
        def byte(b, ack):
            nonlocal sda, scl
            for i in range(7, -1, -1):
                sda = (b >> i) & 1; emit(); scl = 1; emit(); scl = 0; emit()
            sda = 0 if ack else 1; emit(); scl = 1; emit(); scl = 0; emit()
        byte((addr << 1) | rw, True)
        for j, d in enumerate(data):
            byte(d, not (nack_last and j == len(data) - 1))
        sda = 0; emit(); scl = 1; emit(); sda = 1; emit(idle)  # stop
    return bytes(out)

def spi(transfers, half=5, idle=50):
    out = []
    clk, mosi, miso, cs = 0, 0, 0, 1
    def emit(n=half):
        out.extend([(clk << 2) | (mosi << 3) | (miso << 4) | (cs << 5)] * n)
    emit(idle)
    for tx, rx in transfers:
        cs = 0; emit()
        for a, b in zip(tx, rx):
            for i in range(7, -1, -1):
                mosi, miso = (a >> i) & 1, (b >> i) & 1; emit(); clk = 1; emit(); clk = 0
        emit(); cs = 1; emit(idle)
    return bytes(out)
