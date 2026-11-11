// Common chips per 7-bit I2C address: suggestions when you name an address in the Bus tab (never applied by
// themselves: many chips share an address).
const LIST: [number[], string[]][] = [
  [[0x10], ['VEML7700', 'VEML6075']],
  [[0x18, 0x19], ['LIS3DH', 'LIS2DH12', 'MCP9808']],
  [[0x1c, 0x1d], ['LIS3MDL', 'MMA8452', 'ADXL345']],
  [[0x1e], ['HMC5883L', 'LIS2MDL']],
  [[0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27], ['MCP23017', 'PCF8574', 'TCA9535']],
  [[0x23], ['BH1750']],
  [[0x29], ['VL53L0X', 'TSL2591', 'APDS-9960']],
  [[0x36], ['MAX17048', 'MAX17043']],
  [[0x38], ['AHT20', 'FT6206']],
  [[0x39], ['AS7343', 'APDS-9960', 'TSL2561']],
  [[0x3c, 0x3d], ['SSD1306', 'SH1106']],
  [[0x40, 0x41, 0x44, 0x45], ['INA219', 'INA226', 'HDC1080']],
  [[0x44, 0x45], ['SHT4x', 'SHT3x']],
  [[0x48, 0x49, 0x4a, 0x4b], ['ADS1115', 'TMP102', 'PCT2075']],
  [[0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57], ['24Cxx EEPROM', 'AT24C32']],
  [[0x51], ['PCF8563', 'BM8563']],
  [[0x52], ['VL53L1X']],
  [[0x53], ['ADXL345']],
  [[0x55], ['BQ27441', 'MAX17055']],
  [[0x57], ['MAX30102']],
  [[0x5a, 0x5b], ['CCS811', 'MLX90614', 'DRV2605']],
  [[0x5c], ['AM2320', 'LPS22HB']],
  [[0x60], ['ATECC608', 'MCP4725', 'SI1145']],
  [[0x62], ['SCD40', 'SCD41']],
  [[0x68], ['DS3231', 'MPU6050', 'ICM-20948']],
  [[0x69], ['MPU6050', 'ICM-20948', 'SPS30']],
  [[0x6a, 0x6b], ['LSM6DS3', 'LSM6DSOX', 'BQ25895']],
  [[0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77], ['TCA9548A', 'PCA9546', 'HT16K33']],
  [[0x76, 0x77], ['BME280', 'BMP280', 'BME680']],
];

export function suggestions(addr: number): string[] {
  return [...new Set(LIST.filter(([as]) => as.includes(addr)).flatMap(([, names]) => names))].reverse().slice(0, 6);
}
