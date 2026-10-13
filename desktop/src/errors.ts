import { StackError } from "./stack";

/** The one place an arbitrary exception becomes the UI's named error string. */
export function errorName(e: unknown, action: string): string {
  if (e instanceof StackError) return e.name;
  if (e instanceof Error && typeof (e as unknown as { code?: unknown }).code === "string") {
    return (e as unknown as { code: string }).code;
  }
  return `${action}_failed`;
}
