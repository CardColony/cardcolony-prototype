/* esm/flow/composer-submission.js —— 提交词汇表（dsh `contract/composer-submission.ts` 移植）
 *
 * 出处：deepseek-harness/packages/client/ui-conversation/src/client/contract/composer-submission.ts
 *       ＋ src/submission-settings.ts（BUSY_ENTER_BEHAVIORS / DEFAULT_BUSY_ENTER_BEHAVIOR）
 *
 * 只放词表与默认值，无逻辑。
 */

/** 忙时普通 Enter 的可选投递方式（submission-settings.ts:12）。 */
export const BUSY_ENTER_BEHAVIORS = ["queue", "steer"];

/** 普通 Enter 在忙时的默认投递方式（submission-settings.ts:18）。 */
export const DEFAULT_BUSY_ENTER_BEHAVIOR = "queue";

/** 一次普通发送请求的投递方式（composer-submission.ts:8）。 */
export const INPUT_SUBMIT_MODE = { QUEUE: "queue", STEER: "steer" };

/** 键盘手势（composer-submission.ts:11）。 */
export const COMPOSER_SUBMIT_GESTURE = { ENTER: "enter", ACCELERATED: "accelerated" };
