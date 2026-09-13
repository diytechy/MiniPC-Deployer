# MSP430 amplifier relay actuator

Firmware for the MSP-EXP430G2 LaunchPad with an MSP430G2553. It makes P1.4 a
fail-off amplifier command driven through an external S9014 NPN transistor.
It does not drive a relay coil; it pulls the separate relay module's `TRIG`
input toward `U-`.

## Wiring

```text
P1.4 -- 2.2k to 4.7k -- S9014 base
                            |
                         10k to 100k
                            |
LaunchPad GND ----------- S9014 emitter -------- relay U-
relay TRIG -------------- S9014 collector
```

Confirm the salvaged transistor's pinout with diode mode. S9014 lead order is
not sufficiently consistent across vendors to infer it from the part number.
Do not connect relay `U+`, a relay coil, or an amplifier voltage to an MSP430
pin. Keep mains disconnected while identifying `TRIG` polarity.

This circuit requires an **active-low** trigger: briefly joining `TRIG` to
`U-` must activate the relay. If that test does not activate it, stop rather
than adapting the circuit by guesswork.

## Protocol and fail-off behavior

The LaunchPad backchannel UART is 9600 baud, 8-N-1. Commands and replies are:

```text
SET 1  -> STATE 1
SET 0  -> STATE 0
```

An ON command leases P1.4 for about 3.2 seconds. The Linux service renews it
once per second. Missing commands, malformed input, MCU reset, or removal of
USB power drives P1.4 low. The external base-to-emitter resistor covers the
high-impedance interval during reset.

## Build and flash on Linux

Install a GCC toolchain that provides `msp430-elf-gcc` and install `mspdebug`.
Then, from this directory:

```sh
make
make flash
```

The original MSP-EXP430G2 uses the `rf2500` mspdebug driver. Before relying on
the UART, put the LaunchPad's TXD/RXD jumpers in the orientation required for
the G2553 hardware UART and verify the enumerated TTY with:

```sh
ls -l /dev/serial/by-id/
udevadm info --query=property --name=/dev/ttyUSB0
```

Use the resulting stable `/dev/serial/by-id/...` path for
`WALL_AMP_MSP430_DEVICE`. Do not commit a guessed VID/PID udev rule: the old
LaunchPad and the later G2ET expose different debug hardware.
