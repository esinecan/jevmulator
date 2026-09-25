"""sys1: the harnessed mode served on ``/sys1``.

A harnessed agent researches a request with the tools its profile grants, then submits a
distribution for each question through the daemon's form. The daemon validates the form,
computes every derived field, and answers in the pinned Jev response shape. The bare mode
on ``/v1/systemone`` never imports this package.
"""
