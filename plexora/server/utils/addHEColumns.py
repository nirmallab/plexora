#%%
import polars as pl

data = pl.read_csv('./unmicst-WD-76845-097-ij_subtracted_50.csv')
data.head()
#%%
data = data.with_columns(pl.lit(0).alias('HE_r'), pl.lit(0).alias('HE_g'), pl.lit(0).alias('HE_b'))
data.columns
#%%
data.write_csv('./unmicst-WD-76845-097-ij_subtracted_50-jj.csv')
#%%
