import { useEffect, useState, type ImgHTMLAttributes } from "react";

import { downloadResponse } from "../api/client";
import {
  acquireAuthenticatedResource,
  peekAuthenticatedResource,
  prefetchAuthenticatedResource as prefetchCachedAuthenticatedResource,
} from "../utils/authenticatedResourceCache";

interface ObjectUrlState {
  url: string | null;
  loading: boolean;
  error: string | null;
}

interface ResourceState extends ObjectUrlState {
  resourcePath: string | null;
}

const EMPTY_STATE: ResourceState = {
  resourcePath: null,
  url: null,
  loading: false,
  error: null,
};

function loadAuthenticatedResource(resourcePath: string, signal: AbortSignal) {
  return downloadResponse(resourcePath, { signal });
}

export function prefetchAuthenticatedResource(resourcePath: string): Promise<void> {
  return prefetchCachedAuthenticatedResource(resourcePath, loadAuthenticatedResource);
}

export function useAuthenticatedObjectUrl(
  resourcePath: string | null | undefined,
): ObjectUrlState {
  const normalizedPath = resourcePath ?? null;
  const [state, setState] = useState<ResourceState>(EMPTY_STATE);
  const cachedUrl = normalizedPath
    ? peekAuthenticatedResource(normalizedPath)
    : null;

  useEffect(() => {
    if (!normalizedPath) {
      setState(EMPTY_STATE);
      return;
    }

    let active = true;
    let release: (() => void) | null = null;
    const immediateUrl = peekAuthenticatedResource(normalizedPath);
    setState({
      resourcePath: normalizedPath,
      url: immediateUrl,
      loading: immediateUrl === null,
      error: null,
    });
    void acquireAuthenticatedResource(normalizedPath, loadAuthenticatedResource)
      .then((lease) => {
        if (!active) {
          lease.release();
          return;
        }
        release = lease.release;
        setState({
          resourcePath: normalizedPath,
          url: lease.url,
          loading: false,
          error: null,
        });
      })
      .catch((reason: unknown) => {
        if (!active || (reason instanceof DOMException && reason.name === "AbortError")) return;
        setState({
          resourcePath: normalizedPath,
          url: null,
          loading: false,
          error: reason instanceof Error ? reason.message : "이미지를 불러오지 못했습니다.",
        });
      });

    return () => {
      active = false;
      release?.();
    };
  }, [normalizedPath]);

  if (cachedUrl) return { url: cachedUrl, loading: false, error: null };
  if (state.resourcePath === normalizedPath) return state;
  return { url: null, loading: normalizedPath !== null, error: null };
}

interface AuthenticatedImageProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> {
  resourcePath: string | null | undefined;
}

export function AuthenticatedImage({
  resourcePath,
  ...props
}: AuthenticatedImageProps) {
  const resource = useAuthenticatedObjectUrl(resourcePath);
  return (
    <img
      {...props}
      src={resource.url ?? undefined}
      aria-busy={resource.loading || undefined}
      data-load-error={resource.error ?? undefined}
    />
  );
}
