import React from "react";

/** Props for App. */
export interface Props {
  title: string;
}

/** The app root. */
export const App = (props: Props) => <div>{props.title}</div>;
