import type { AuthUser } from "../store/auth";

export interface AccountPresentation {
  label: string;
  initials: string;
}

function initialsFromLabel(label: string): string {
  const parts = label.match(/[\p{L}\p{N}]+/gu) ?? [];

  if (parts.length >= 2) {
    return parts
      .slice(0, 2)
      .map((part) => Array.from(part)[0])
      .join("")
      .toUpperCase();
  }

  return Array.from(parts[0] ?? "AD")
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

function accountIdFrom(value: string | null | undefined): string | null {
  const accountId = value?.trim().split("@", 1)[0]?.trim();
  return accountId || null;
}

export function getAccountPresentation(
  user: AuthUser | null | undefined,
): AccountPresentation {
  const username = accountIdFrom(user?.username);
  if (username) {
    return { label: username, initials: initialsFromLabel(username) };
  }

  const emailHandle = accountIdFrom(user?.email);
  if (emailHandle) {
    return { label: emailHandle, initials: initialsFromLabel(emailHandle) };
  }

  if (user) {
    const generatedId = `user-${user.id}`;
    return { label: generatedId, initials: initialsFromLabel(generatedId) };
  }

  return { label: "annodock", initials: "AD" };
}
