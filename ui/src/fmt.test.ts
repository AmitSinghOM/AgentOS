import { describe, expect, it } from "vitest";
import { eventFamily, fmtAgo, fmtClock, fmtDateTime, fmtDelta, shortHash } from "./fmt";

describe("fmt", () => {
  it("clock and datetime render local wall time and keep milliseconds", () => {
    const iso = "2026-09-20T00:00:01.250Z";
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    expect(fmtClock(iso)).toMatch(new RegExp(`^${hh}:\\d\\d:01\\.250$`));
    expect(fmtDateTime(iso)).toMatch(/^\d{4}-\d\d-\d\d \d\d:\d\d:01$/);
  });
  it("degrades to the raw input when it cannot parse, never 'Invalid Date'", () => {
    expect(fmtClock("t3")).toBe("t3");
    expect(fmtDateTime("nope")).toBe("nope");
    expect(fmtAgo("nope")).toBe("nope");
    expect(fmtClock(null)).toBe("—");
    expect(fmtDelta("t1", "t2")).toBeNull();
  });
  it("relative time picks the unit", () => {
    const now = Date.parse("2026-09-20T12:00:00Z");
    expect(fmtAgo("2026-09-20T11:59:48Z", now)).toBe("12 s ago");
    expect(fmtAgo("2026-09-20T11:57:00Z", now)).toBe("3 min ago");
    expect(fmtAgo("2026-09-20T10:00:00Z", now)).toBe("2 h ago");
    expect(fmtAgo("2026-09-16T12:00:00Z", now)).toBe("4 d ago");
    expect(fmtAgo("2026-09-20T12:00:05Z", now)).toBe("0 s ago");   // clock skew never goes negative
  });
  it("delta between rows is signed and unit-scaled", () => {
    expect(fmtDelta("2026-09-20T00:00:00.000Z", "2026-09-20T00:00:00.012Z")).toBe("+12 ms");
    expect(fmtDelta("2026-09-20T00:00:00Z", "2026-09-20T00:00:02.300Z")).toBe("+2.30 s");
    expect(fmtDelta("2026-09-20T00:00:00Z", "2026-09-20T00:01:05Z")).toBe("+1 min 5 s");
    expect(fmtDelta("2026-09-20T00:00:01Z", "2026-09-20T00:00:00Z")).toBe("−1.00 s");
  });
  it("hash and family helpers", () => {
    expect(shortHash("e9fd047b301f9999")).toBe("e9fd047b301f");
    expect(shortHash(null)).toBe("—");
    expect(eventFamily("governance.policy_applied")).toBe("governance");
    expect(eventFamily("weird")).toBe("weird");
  });
});
