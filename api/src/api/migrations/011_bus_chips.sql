-- Chips on a board's I2C bus, named by you in the Bus tab: {"m:68": {"name": "SHT40"}, "0:16": {"name": "VEML7700"},
-- "m:112": {"name": "I2C switch", "switch": true}}. The key is where the chip sits ("m" = main bus, 0-7 = a port
-- of an I2C switch) and its 7-bit address.
ALTER TABLE devices ADD COLUMN bus_chips jsonb NOT NULL DEFAULT '{}';
