"""Server side of Figure Builder.

Split the same way ROI's is, and for the same reason: `schema` says what a
figure IS, `operations` says what may be done to one, `repository` says where it
lives and who may overwrite whom, and `routes` only translates. One place to
read each rule, and no route that can quietly disagree with another.
"""
