export interface TenantMembership {
  id: string;
  name: string;
  role: "owner" | "admin" | "member";
}

export const ACTIVE_TENANT_KEY = "leadhunter.active_tenant";

export function tenantStorageKey(userId: string): string {
  return `leadhunter.tenant.${userId}`;
}

export function chooseTenant(
  memberships: ReadonlyArray<Pick<TenantMembership, "id">>,
  savedId: string,
): string {
  if (savedId && memberships.some((tenant) => tenant.id === savedId)) return savedId;
  return memberships[0]?.id ?? "";
}
