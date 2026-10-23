import dayjs from 'dayjs';

/**
 * Format a backend timestamp in the browser's local timezone.
 *
 * Backend stores naive UTC datetimes (datetime.utcnow) and serializes them
 * without a timezone suffix; dayjs would parse those as local time (8h off
 * in UTC+8). Append "Z" when no timezone info is present so the value is
 * interpreted as UTC and rendered in local time.
 */
export function formatBackendTime(
  val: string | null | undefined,
  fmt = 'YYYY-MM-DD HH:mm'
): string {
  if (!val) return '-';
  const hasTz = /Z$|[+-]\d{2}:\d{2}$/.test(val);
  return dayjs(hasTz ? val : `${val}Z`).format(fmt);
}
