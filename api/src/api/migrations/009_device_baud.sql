-- Every device has a UART speed (no more "keep current"): the ones without get 115200, the old UART_BAUD default.
UPDATE devices SET uart_baud = 115200 WHERE uart_baud IS NULL;
ALTER TABLE devices ALTER COLUMN uart_baud SET DEFAULT 115200;
ALTER TABLE devices ALTER COLUMN uart_baud SET NOT NULL;
