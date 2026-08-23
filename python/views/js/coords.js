/* Coordinate helpers shared by the GPS and Locations pages. */

/** Accept both period and comma as the decimal separator; return period form. */
function normalizeDecimal(value) {
  if (value === null || value === undefined) return '';
  return String(value).trim().replace(',', '.');
}

/**
 * Decimal degrees -> [degrees, minutes, seconds].
 * The sign lives on the degrees value; minutes and seconds are positive.
 * -3.5 becomes [-3, 30, 0] (not [-4, 30, 0]).
 */
function decimalToDMS(decimal) {
  var sign = decimal < 0 ? -1 : 1;
  var abs = Math.abs(decimal);
  var degrees = Math.trunc(abs);
  var minutes = Math.trunc((abs - degrees) * 60);
  var seconds = (abs - degrees - minutes / 60) * 3600;
  seconds = Math.round(seconds * 100) / 100;
  if (seconds >= 60) {
    seconds = 0;
    minutes += 1;
  }
  if (minutes >= 60) {
    minutes = 0;
    degrees += 1;
  }
  return [sign * degrees, minutes, seconds];
}

/** [degrees, minutes, seconds] -> decimal degrees. The sign comes from degrees. */
function DMSToDecimal(degrees, minutes, seconds) {
  var degText = normalizeDecimal(degrees);
  degrees = parseFloat(degText) || 0;
  minutes = parseFloat(normalizeDecimal(minutes)) || 0;
  seconds = parseFloat(normalizeDecimal(seconds)) || 0;
  if (minutes < 0 || minutes >= 60) minutes = Math.abs(minutes) % 60;
  if (seconds < 0 || seconds >= 60) seconds = Math.abs(seconds) % 60;
  var sign = degrees < 0 || degText.startsWith('-') ? -1 : 1;
  return sign * (Math.abs(degrees) + minutes / 60 + seconds / 3600);
}

/** Range check for one coordinate field. Returns true when valid. */
function validateField(value, fieldName) {
  value = normalizeDecimal(value);
  if (value === '') return false;
  var numValue = parseFloat(value);
  if (isNaN(numValue)) return false;
  switch (fieldName) {
    case 'latitude':
      return numValue >= -90 && numValue <= 90;
    case 'longitude':
      return numValue >= -180 && numValue <= 180;
    case 'altitude':
      return numValue >= -1000 && numValue <= 10000;
    case 'error_in_m':
      return numValue >= 0 && numValue <= 10000;
    default:
      return true;
  }
}
