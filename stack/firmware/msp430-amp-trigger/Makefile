MSP430_CC ?= msp430-elf-gcc
MSPDEBUG ?= mspdebug
MCU := msp430g2553
TARGET := wall-amp-trigger

CFLAGS := -mmcu=$(MCU) -Os -std=c99 -Wall -Wextra -Werror

.PHONY: all flash clean

all: $(TARGET).elf

$(TARGET).elf: main.c
	$(MSP430_CC) $(CFLAGS) -o $@ $<

flash: $(TARGET).elf
	$(MSPDEBUG) rf2500 "prog $(TARGET).elf"

clean:
	rm -f $(TARGET).elf
