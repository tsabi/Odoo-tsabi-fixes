declare module "models" {
    import { Activity as ActivityClass } from "@mail/core/web/activity_model";

    export interface Activity extends ActivityClass {}
    export interface Store {
        activityCounter: number,
        activityGroups: Object[],
    }
    export interface Thread {
        recipients: Follower[],
    }

    export interface Models {
        "Activity": Activity,
    }
}
