import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { rotateTeamKey, type ApiKey, type IssuedKey } from "@/features/api-keys/api";

interface RotateKeyDialogProps {
  /** The key to rotate, or null when closed. Carries its own team_id. */
  apiKey: ApiKey | null;
  onOpenChange: (open: boolean) => void;
}

/** Rotate a key: issue a replacement and start the old key's grace window (it
 * keeps working for ~1h). The new plaintext is shown once. */
export function RotateKeyDialog({ apiKey, onOpenChange }: RotateKeyDialogProps) {
  const queryClient = useQueryClient();
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setIssued(null);
    setCopied(false);
  }, [apiKey]);

  const mutation = useMutation({
    mutationFn: (key: ApiKey) => rotateTeamKey(key.team_id, key.id),
    onSuccess: async (key, oldKey) => {
      await queryClient.invalidateQueries({ queryKey: ["team-keys", oldKey.team_id] });
      setIssued(key);
    },
  });

  async function copy() {
    if (!issued) return;
    await navigator.clipboard.writeText(issued.plaintext);
    setCopied(true);
  }

  return (
    <Dialog
      open={apiKey !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        {issued ? (
          <>
            <DialogHeader>
              <DialogTitle>Key rotated</DialogTitle>
              <DialogDescription>
                Here is the new key — copy it now (shown once). The old key keeps working for
                about an hour, then stops.
              </DialogDescription>
            </DialogHeader>
            <div className="flex items-center gap-2 rounded-md border border-border bg-secondary p-2">
              <code className="flex-1 break-all font-mono text-xs text-foreground">
                {issued.plaintext}
              </code>
              <Button type="button" variant="outline" size="sm" onClick={copy}>
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                {copied ? "copied" : "copy"}
              </Button>
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                done
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Rotate API key</DialogTitle>
              <DialogDescription>
                Issue a replacement for{" "}
                <span className="font-mono text-foreground">{apiKey?.name ?? apiKey?.prefix}</span>{" "}
                (same scope and rate limit). The old key keeps working for ~1h so clients can
                migrate, then stops. For an immediate cut-over, revoke instead.
              </DialogDescription>
            </DialogHeader>
            {mutation.isError ? (
              <p className="font-mono text-xs text-destructive">{mutation.error.message}</p>
            ) : null}
            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={mutation.isPending}
              >
                cancel
              </Button>
              <Button
                type="button"
                disabled={mutation.isPending || apiKey === null}
                onClick={() => apiKey && mutation.mutate(apiKey)}
              >
                {mutation.isPending ? "rotating…" : "rotate"}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
