import { ApiError } from "@/lib/api/client";

/** A refused or failed write, in a sentence a person can act on. */
export function describeWriteError(error: unknown): { title: string; body: string } {
  if (!(error instanceof ApiError)) {
    return { title: "That did not work", body: error instanceof Error ? error.message : "Something went wrong." };
  }
  const detail = error.detail ?? error.message;
  switch (error.status) {
    case 0:
      return { title: "Cannot reach the API", body: error.message };
    case 403:
      return { title: "Not permitted", body: detail };
    case 404:
      return { title: "Not found", body: `${detail} It may have been removed since this page loaded.` };
    case 409:
      return { title: "Not possible right now", body: detail };
    case 422:
      return { title: "The API did not accept these values", body: detail };
    case 429:
      return { title: "Too many requests", body: error.message };
    default:
      return { title: `The API answered ${error.status}`, body: error.message };
  }
}
