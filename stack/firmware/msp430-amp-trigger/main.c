/* MSP430G2553 fail-off amplifier relay actuator.
 *
 * P1.4 drives an S9014 base through 2.2k--4.7k. The S9014 emitter and the
 * relay module U- share ground; its collector pulls TRIG low. A 10k--100k
 * base-to-emitter resistor keeps the relay off while this MCU resets.
 *
 * The host sends exact 9600-8-N-1 lines: "SET 1\n" and "SET 0\n". ON is a
 * renewable lease, not a latch. If commands stop for about 3.2 seconds the
 * watchdog interrupt clears P1.4. This covers a wedged Linux daemon while
 * USB power remains present.
 */

#include <msp430.h>
#include <string.h>

#define AMP_OUTPUT BIT4
#define LEASE_TICKS 100u       /* 100 * 32 ms = approximately 3.2 seconds. */
#define LINE_CAPACITY 8u

static volatile unsigned char lease_ticks;

static void uart_puts(const char *text)
{
    while (*text != '\0') {
        while (!(IFG2 & UCA0TXIFG)) {
        }
        UCA0TXBUF = (unsigned char)*text++;
    }
}

static void set_amplifier(unsigned char enabled)
{
    if (enabled) {
        lease_ticks = LEASE_TICKS;
        P1OUT |= AMP_OUTPUT;
        uart_puts("STATE 1\n");
    } else {
        P1OUT &= ~AMP_OUTPUT;
        lease_ticks = 0;
        uart_puts("STATE 0\n");
    }
}

#pragma vector=WDT_VECTOR
__interrupt void watchdog_interval(void)
{
    if (lease_ticks != 0) {
        --lease_ticks;
        if (lease_ticks == 0) {
            P1OUT &= ~AMP_OUTPUT;
        }
    }
    __bic_SR_register_on_exit(LPM0_bits);
}

int main(void)
{
    char line[LINE_CAPACITY];
    unsigned char used = 0;

    /* Establish the safe level before making P1.4 an output. */
    P1OUT &= ~AMP_OUTPUT;
    P1DIR |= AMP_OUTPUT;
    P1SEL &= ~AMP_OUTPUT;
    P1SEL2 &= ~AMP_OUTPUT;

    if (CALBC1_1MHZ == 0xff) {
        /* Calibration erased: stay physically off rather than improvise UART. */
        for (;;) {
            __bis_SR_register(LPM4_bits);
        }
    }
    DCOCTL = 0;
    BCSCTL1 = CALBC1_1MHZ;
    DCOCTL = CALDCO_1MHZ;

    P1SEL |= BIT1 | BIT2;
    P1SEL2 |= BIT1 | BIT2;
    UCA0CTL1 = UCSWRST | UCSSEL_2;
    UCA0BR0 = 104;
    UCA0BR1 = 0;
    UCA0MCTL = UCBRS0;
    UCA0CTL1 &= ~UCSWRST;

    WDTCTL = WDT_MDLY_32;
    IE1 |= WDTIE;
    __enable_interrupt();
    uart_puts("READY AMP1\n");

    for (;;) {
        char byte;
        if (!(IFG2 & UCA0RXIFG)) {
            __bis_SR_register(LPM0_bits | GIE);
            __no_operation();
            continue;
        }
        byte = (char)UCA0RXBUF;
        if (byte == '\r') {
            continue;
        }
        if (byte != '\n' && used < LINE_CAPACITY - 1u) {
            line[used++] = byte;
            continue;
        }
        line[used] = '\0';
        if (byte == '\n' && strcmp(line, "SET 1") == 0) {
            set_amplifier(1);
        } else if (byte == '\n' && strcmp(line, "SET 0") == 0) {
            set_amplifier(0);
        } else {
            P1OUT &= ~AMP_OUTPUT;
            lease_ticks = 0;
            uart_puts("ERR\n");
        }
        used = 0;
    }
}
