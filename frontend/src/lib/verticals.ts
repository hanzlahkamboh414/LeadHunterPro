/** Product access from the persisted signup category. Open mode is shared. */
export function canSeePhones(category?: string | null, isAdmin = false, userExists = false): boolean {
  return !userExists || isAdmin || category === "phones" || category === "both";
}

export function canSeeEmails(category?: string | null, isAdmin = false, userExists = false): boolean {
  return !userExists || isAdmin || category === "emails" || category === "both";
}

export function isPhoneOnly(category?: string | null, isAdmin = false, userExists = false): boolean {
  return canSeePhones(category, isAdmin, userExists) && !canSeeEmails(category, isAdmin, userExists);
}
