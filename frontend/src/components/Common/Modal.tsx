// =============================================================================
// KHAOS 量化交易系统 - 通用模态框 v3.0 (终极版)
// =============================================================================
// 修复: 并发安全、动画同步、4K 适配、焦点增强、多模态协调
// 适用: 2000 美金至万亿美金账户，4K 中文界面，移动端
// =============================================================================

import React, {
  useEffect, useRef, useCallback, useState, useMemo,
  ReactNode, useId
} from 'react';
import { createPortal } from 'react-dom';

// ===========================
// 类型
// ===========================
export type ModalSize = 'sm' | 'md' | 'lg' | 'xl' | 'full';

export interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  size?: ModalSize;
  showCloseButton?: boolean;
  closeOnOverlayClick?: boolean;
  closeOnEscape?: boolean;
  footer?: ReactNode;
  className?: string;
  ariaLabel?: string;
  ariaDescribedby?: string;
  onAfterOpen?: () => void;
  onAfterClose?: () => void;
  initialFocusRef?: React.RefObject<HTMLElement>;
  finalFocusRef?: React.RefObject<HTMLElement>;
  disableFocusTrap?: boolean;
  stickyFooter?: boolean;
  /** 指定 portal 目标，默认 document.body */
  container?: HTMLElement | null;
  /** 禁用背景滚动锁定 */
  disableScrollLock?: boolean;
}

// ===========================
// 全局计数器与 z-index
// ===========================
let modalCount = 0;
let baseZIndex = 1000;

function getNextZIndex() {
  return baseZIndex + modalCount * 10;
}

// 焦点选择器
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), iframe, [tabindex]:not([tabindex="-1"]), summary, [contenteditable], [role="button"], [role="link"], audio[controls], video[controls]';

// ===========================
// 工具
// ===========================
const isBrowser = typeof document !== 'undefined';

function safeInvoke(fn?: () => void) {
  try { fn?.(); } catch (e) { console.error('[Modal] 回调错误:', e); }
}

function isElementInDOM(el: HTMLElement | null): boolean {
  if (!el || !isBrowser) return false;
  return document.body.contains(el);
}

const Modal: React.FC<ModalProps> = (props) => {
  const {
    isOpen, onClose, title, children, size = 'md',
    showCloseButton = true, closeOnOverlayClick = true,
    closeOnEscape = true, footer, className = '',
    ariaLabel, ariaDescribedby,
    onAfterOpen, onAfterClose,
    initialFocusRef, finalFocusRef,
    disableFocusTrap = false, stickyFooter = false,
    container = isBrowser ? document.body : null,
    disableScrollLock = false,
  } = props;

  // --- 状态机 ---
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);
  const [closing, setClosing] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);

  // --- Refs ---
  const overlayRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const previousActiveElement = useRef<HTMLElement | null>(null);
  const savedBodyOverflow = useRef<string>('');
  const scrollBarWidth = useRef<number>(0);
  const closeToken = useRef(0);
  const onCloseRef = useRef(onClose);
  const mountedRef = useRef(false);
  const afterCallbacksRef = useRef({ open: onAfterOpen, close: onAfterClose });
  afterCallbacksRef.current = { open: onAfterOpen, close: onAfterClose };

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  // 系统动画偏好
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReducedMotion(mq.matches);
    update();
    mq.addEventListener('change', update);
    return () => mq.removeEventListener('change', update);
  }, []);

  // 滚动条宽度补偿
  useEffect(() => {
    if (isBrowser) {
      scrollBarWidth.current = window.innerWidth - document.documentElement.clientWidth;
    }
  }, []);

  // 唯一 ID
  const generatedId = useId();
  const titleId = useMemo(() => `modal-title-${generatedId}`, [generatedId]);
  const bodyId = useMemo(() => `modal-body-${generatedId}`, [generatedId]);

  // 打开/关闭逻辑
  useEffect(() => {
    if (isOpen && !mounted) {
      setMounted(true);
      mountedRef.current = true;
      modalCount++;
      // 保存焦点
      previousActiveElement.current = document.activeElement as HTMLElement;
      // 背景锁定
      if (!disableScrollLock) {
        savedBodyOverflow.current = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        document.body.style.paddingRight = `${scrollBarWidth.current}px`;
        const root = document.getElementById('khaos-app-root');
        if (root && modalCount === 1) root.setAttribute('aria-hidden', 'true');
      }
      // 动画进入
      requestAnimationFrame(() => {
        setVisible(true);
      });
    } else if (!isOpen && mounted && !closing) {
      setClosing(true);
      setVisible(false);
      const token = ++closeToken.current;
      // 动画结束后清理
      const cleanup = () => {
        if (token !== closeToken.current) return;
        setMounted(false);
        setClosing(false);
        mountedRef.current = false;
        modalCount = Math.max(0, modalCount - 1);
        if (!disableScrollLock) {
          if (modalCount <= 0) {
            document.body.style.overflow = savedBodyOverflow.current;
            document.body.style.paddingRight = '';
            const root = document.getElementById('khaos-app-root');
            if (root) root.removeAttribute('aria-hidden');
          }
        }
        const target = finalFocusRef?.current || previousActiveElement.current;
        if (target && isElementInDOM(target)) {
          setTimeout(() => target.focus?.(), 0);
        }
        safeInvoke(afterCallbacksRef.current.close);
      };
      if (reducedMotion) {
        cleanup();
      } else {
        const timer = setTimeout(cleanup, 200);
        return () => clearTimeout(timer);
      }
    }
  }, [isOpen, mounted, closing, disableScrollLock, reducedMotion, finalFocusRef]);

  // 打开动画完成后回调
  useEffect(() => {
    if (visible && !closing && mounted) {
      const timer = setTimeout(() => {
        safeInvoke(afterCallbacksRef.current.open);
      }, reducedMotion ? 0 : 200);
      return () => clearTimeout(timer);
    }
  }, [visible, closing, mounted, reducedMotion]);

  // 关闭函数
  const close = useCallback(() => {
    if (closing) return;
    onCloseRef.current();
  }, [closing]);

  // 键盘事件
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (closing) return;
    if (closeOnEscape && e.key === 'Escape') {
      const active = document.activeElement as HTMLElement | null;
      if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
      e.stopPropagation();
      close();
      return;
    }
    if (disableFocusTrap || !contentRef.current) return;
    if (e.key === 'Tab') {
      const focusable = Array.from(contentRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }, [closeOnEscape, disableFocusTrap, closing, close]);

  // 点击遮罩
  const handleOverlayClick = useCallback((e: React.MouseEvent) => {
    if (closing) return;
    if (closeOnOverlayClick && (e.target as HTMLElement).closest('[data-modal-overlay]') === overlayRef.current) {
      close();
    }
  }, [closeOnOverlayClick, closing, close]);

  // 初始焦点
  useEffect(() => {
    if (visible && contentRef.current && !closing) {
      const target = initialFocusRef?.current && contentRef.current.contains(initialFocusRef.current)
        ? initialFocusRef.current
        : contentRef.current.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) || contentRef.current;
      target.focus?.();
    }
  }, [visible, closing, initialFocusRef]);

  // 卸载清理
  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  if (!mounted && !isOpen) return null;
  if (!container && !isBrowser) return null;

  const zIndex = getNextZIndex();
  const duration = reducedMotion ? '0s' : '0.2s';

  const overlayStyle: React.CSSProperties = {
    position: 'fixed',
    inset: 0,
    backgroundColor: 'rgba(0,0,0,0.6)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex,
    opacity: visible ? 1 : 0,
    transition: reducedMotion ? 'none' : `opacity ${duration} ease`,
  };

  const contentStyle: React.CSSProperties = {
    backgroundColor: 'var(--color-dark-surface)',
    border: '1px solid var(--color-border)',
    borderRadius: '12px',
    padding: '24px',
    width: '100%',
    maxHeight: '80vh',
    overflowY: 'auto',
    overscrollBehavior: 'contain',
    boxShadow: '0 4px 24px rgba(0,0,0,0.4)',
    transform: visible ? 'translateY(0)' : 'translateY(20px)',
    opacity: visible ? 1 : 0,
    transition: reducedMotion ? 'none' : `transform ${duration} ease, opacity ${duration} ease`,
    display: 'flex',
    flexDirection: 'column',
    position: 'relative',
  };

  return createPortal(
    <div
      ref={overlayRef}
      style={overlayStyle}
      data-modal-overlay
      onClick={handleOverlayClick}
      onKeyDown={handleKeyDown}
      role="presentation"
    >
      <div
        ref={contentRef}
        className={className}
        style={contentStyle}
        role="dialog"
        aria-modal={!disableFocusTrap}
        aria-labelledby={titleId}
        aria-label={!title ? ariaLabel : undefined}
        aria-describedby={ariaDescribedby || (disableFocusTrap ? undefined : bodyId)}
        tabIndex={-1}
      >
        {(title || showCloseButton) && (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <h2 id={titleId} style={{ fontSize: '1.25rem', fontWeight: 600, margin: 0 }}>{title}</h2>
            {showCloseButton && (
              <button onClick={close} aria-label="关闭" type="button" style={{ background: 'none', border: 'none', fontSize: '1.5rem', cursor: 'pointer', color: 'var(--color-text-secondary)' }}>
                ✕
              </button>
            )}
          </div>
        )}
        <div id={bodyId} style={{ flex: 1, overflowY: 'auto' }}>
          {children}
        </div>
        {footer && (
          <div style={{
            marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--color-border)',
            ...(stickyFooter ? { position: 'sticky', bottom: 0, backgroundColor: 'var(--color-dark-surface)' } : {}),
          }}>
            {footer}
          </div>
        )}
      </div>
    </div>,
    container || document.body
  );
};

Modal.displayName = 'Modal';

export default Modal;
